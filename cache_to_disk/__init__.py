"""cache_to_disk: Cache the results of functions persistently on disk

Original Work, Copyright (c) 2018 Stewart Renehan, MIT License
    Author: https://github.com/sarenehan
    Project: https://github.com/sarenehan/cache_to_disk

Modifications:
    Author: https://github.com/mzpqnxow
    Project: https://github.com/mzpqnxow/cache_to_disk/tree/feature/nocache

    This modified version adds the following:
        - Accounting of hits, misses and nocache events
        - cache_info(), cache_clear(), cache_size(), cache_get_raw() interfaces accessible
          via the function itself for convenience
        - NoCacheCondition exception, simple interface for a user to prevent a
          specific function result to not be cached, while still passing a return
          value to the caller
        - Minor refactoring of the decorator, for easier reading
        - Minor refactoring of delete_old_disk_caches(), to reduce logical blocks
          and depth of indentation
        - Default cache age value (DEFAULT_CACHE_AGE)
        - Special unlimited age value (UNLIMITED_CACHE_AGE)
        - Use of logging module (but defaulting to NullAdapter)
        - Minor PEP8 / cosmetic changes
        - Minor cosmetic changes to file path generation (use of os.path.join, a constant
          for the directory/file path)
        - Support getting cache directory or filename from environment:
            Cache metadata filename: $DISK_CACHE_FILENAME
            Base directory for cache files: $DISK_CACHE_DIR
        - Expansion of shell variables and tilde-user values for directories/files
"""
# Standard Library
import fcntl
import json
import logging
import os
import pickle
import warnings
from collections import namedtuple
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from os import getenv
from os.path import (
    dirname,
    exists as file_exists,
    expanduser,
    expandvars,
    getmtime,
    isfile,
    join as join_path,
    realpath,
)
from time import sleep
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

import logging,os
from datetime import datetime
import shutil
import uuid

def get_logger(name,console_enable = False,propagate = False, log_dir='/mnt/disk1/log'):
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    # formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    # formatter = logging.Formatter('%(asctime)s [PID %(process)d] %(levelname)s %(name)s - %(message)s')
    formatter = logging.Formatter('%(asctime)s [PID %(process)d] %(levelname)s (%(name)s::%(funcName)s:%(lineno)d) - %(message)s')
    # remove all handlers
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    # set log handler to save to file
    now_str = datetime.now().strftime("%Y-%m-%d")
    # print(f"Logging to {log_dir}")
    # print(f"now {datetime.now()}")
    # print(f"python version {os.popen('python --version').read()}")
    # print(f"python path {os.popen('which python').read()}")
    # create log dir if not exist
    try:
        os.makedirs(log_dir, exist_ok=True)
    except Exception as e:
        print(f"Failed to create log directory {log_dir}: {e}")
        return None

    log_file = f"{log_dir}/{name}_{now_str}.log"
    # print(f"Logging to {log_file}")
    try:
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        # print(f"Logging to file {log_file}")
    except Exception as e:
        print(f"Failed to add file handler: {e}")
        return None
    if console_enable:
        try:
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.WARNING)
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)
            print(f"Logging to console")
        except Exception as e:
            print(f"Failed to add console handler: {e}")
            # return None
    logger.propagate = propagate
    return logger

# logger = logging.getLogger(__name__)

logger = get_logger(__name__,console_enable = True,propagate = False, log_dir='/mnt/disk1/log')

if logger.handlers is None:
    # Don't log unless user explicitly adds a handler
    logger.addHandler(logging.NullHandler())

MAX_PICKLE_BYTES = 2**31 - 1
DISK_CACHE_DIR = expanduser(
    expandvars(
        getenv("DISK_CACHE_DIR", join_path(dirname(realpath(__file__)), "disk_cache"))
    )
)
DISK_CACHE_FILE = expanduser(
    expandvars(
        join_path(
            DISK_CACHE_DIR, getenv("DISK_CACHE_FILENAME", "cache_to_disk_caches.json")
        )
    )
)
DISK_CACHE_FILE_LOCK = "{}.lock".format(DISK_CACHE_FILE)

# By default, retry lock attempts every 0.1 seconds for up to 5 seconds
DISK_CACHE_LOCK_MAX_WAIT = float(getenv("DISK_CACHE_LOCK_MAX_WAIT", 10))
DISK_CACHE_LOCK_INTERVAL = float(getenv("DISK_CACHE_LOCK_INTERVAL", 0.1))
DEFAULT_CACHE_AGE = int(getenv("DEFAULT_CACHE_AGE", 15))

# Specify 0 for cache age days to keep forever; not recommended for obvious reasons
UNLIMITED_CACHE_AGE = 0

_TOTAL_NUMCACHE_KEY = "total_number_of_cache_to_disks"

# Run-time cache data, stolen from Python functools.lru_cache implementation
# Events resulting in nocache are cache misses that complete, but instruct cache_to_disk to
# not store the result. Useful, for example, in a function that makes a network request and
# experiences a failure that is considered likely to be temporary. This is accomplished in
# the user function by raising NoCacheCondition
_CacheInfo = namedtuple("_CacheInfo", ["hits", "misses", "nocache"])

# This is probably unnecessary ...
# logger.debug('cache_to_disk package loaded; using DISK_CACHE_DIR=%s',
#             os.path.relpath(DISK_CACHE_DIR, '.'))

def get_memmap_random_filepath():
    filename = str(uuid.uuid4())
    return os.path.join(DISK_CACHE_DIR,filename)
    

class NoCacheCondition(Exception):
    """Custom exception for user function to raise to prevent caching on a per-call basis

    The function_value kwarg can be set as a kwarg to return a value other than None to the
    original caller

    Example
    -------
    The following contrived example will return a value to the caller but avoids it being
    cached. In this example, a socket exception is considered a failure, but there is some
    value in returning a partial response to the caller in cases such as SIGPIPE/EPIPE in
    the read loop

    On a socket exception, the function will effectively return either an empty bytes
    buffer or a bytes buffer with partial response data, depending on where the network
    exception occurred

    @cache_to_disk(7)
    def network_query(hostname, port, query):
        response = b''
        try:
            socket = tcp_connect(hostname)
            socket.send(query)
            while True:
                # Build the response incrementally
                buf = read_bytes(socket, 1024)
                if buf is None:
                    break
                response += buf
        except socket.error:
            raise NoCacheCondition(function_value=buf)

        return response
    """

    __slots__ = ["function_value"]

    def __init__(self, function_value: Any = None):
        self.function_value = function_value
        logger.info("NoCacheCondition caught in cache_to_disk")


class LockTimeout(Exception):
    """Failed to acquire a lock after retrying to exhaustion"""


@contextmanager
def open_locked(path: str, mode: str, flags: int):
    """You can use this directly, or you can use the open_exclusive() or open_shared() wrappers"""
    with open("{}.lock".format(path), mode="w") as lockfd:
        elapsed = 0
        while elapsed < DISK_CACHE_LOCK_MAX_WAIT:
            try:
                fcntl.flock(lockfd, flags)
                try:
                    fd = open(path, mode=mode)
                except IOError:
                    # Close to release the lock and raise the exception
                    # Some callers need to know about ENOENT, EPERM, etc.
                    lockfd.close()
                    raise
                yield fd
                fd.close()
                lockfd.close()
                break
            except BlockingIOError:
                logger.warning("Unable to get exclusive (write) lock on %s, retry in %s seconds ...", path, DISK_CACHE_LOCK_INTERVAL)
                sleep(DISK_CACHE_LOCK_INTERVAL)
        else:
            raise LockTimeout("Unable to acquire write lock on {}".format(path))
    # Lock is implicitly released when fd is closed
    return


def open_exclusive(path: str, mode: str = "w"):
    """Non-blocking exclusive lock"""
    return open_locked(path, mode, fcntl.LOCK_EX | fcntl.LOCK_NB)


def open_shared(path: str, mode='r'):
    """Non-blocking shared lock"""
    return open_locked(path, mode, fcntl.LOCK_SH | fcntl.LOCK_NB)


def write_cache_file(cache_metadata_dict: Dict) -> None:
    """Dump an object as JSON to a file"""
    with open_exclusive(DISK_CACHE_FILE) as f:
        return json.dump(cache_metadata_dict, f)


def load_cache_metadata_json() -> Dict:
    """Load a JSON file, create it with empty cache structure if it doesn't exist"""
    try:
        with open_shared(DISK_CACHE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        write_cache_file({_TOTAL_NUMCACHE_KEY: 0})
        return {_TOTAL_NUMCACHE_KEY: 0}


def ensure_dir(directory: str) -> None:
    """Create a directory tree if it doesn't already exist"""
    if not file_exists(directory):
        os.makedirs(directory)
        write_cache_file({_TOTAL_NUMCACHE_KEY: 0})

import numpy as np

def try_save_numpy2(data: Any, file_path: str,rename_np_memmap_file:bool) -> bool:
    # check if data is numpy array
    if isinstance(data, np.ndarray):
        with open_exclusive(file_path, "wb") as f:
            np.save(f, data)
            f.flush()
            os.fsync(f.fileno())   
            
        file_bytes = os.path.getsize(file_path)
        data_size = data.nbytes
        # sleep to ensure file is written to disk
        while file_bytes < data_size:
            sleep(0.1)
            file_bytes = os.path.getsize(file_path)
            logger.warnings(f"File size {file_bytes} < data size {data_size} human readable {np.round(file_bytes/1024/1024,2)}MB < {np.round(data_size/1024/1024,2)}MB")
        
        cahce_ready = False
        
        while not cahce_ready:
            try:
                data = np.load(file_path, mmap_mode='c')
                cahce_ready = True
            except Exception as e:
                logger.warning(f"Failed to load numpy file {file_path} {e}")
                sleep(0.1)
        logger.info(f"Saved numpy file {file_path} size {data.nbytes} human readable {np.round(data.nbytes/1024/1024,2)}MB success")
        return True
    return False


# def save_memmap_metadata(np_data:np.memmap):
#     filename = np_data.filename
#     dtype = np_data.dtype
#     shape = np_data.shape
#     with open(filename.replace('.dat','.json'), 'w') as f:
#         json.dump({'dtype':dtype.name,'shape':shape}, f)
        

def rename_np_memmap(from_filepath, file_path: str,dtype,shape) -> bool:
    meta_config = file_path+'.json'
    with open_exclusive(meta_config, "w") as f:
        # dump meta data
        json.dump({'dtype':dtype.name,'shape':shape},f)
        f.flush()
        os.fsync(f.fileno())
        if from_filepath != file_path:
            logger.info(f"Moving numpy memmap file {from_filepath} to {file_path}")
            
            shutil.move(from_filepath,file_path)
            # os.symlink(from_filepath, file_path)
            logger.info(f"Moved numpy memmap file {from_filepath} to {file_path}")
        if os.path.exists(file_path):
            data = np.memmap(file_path, dtype=dtype, mode='r', shape=shape)
            return data
    return None

def load_np_memmap(file_path: str) -> bool:
    data = None
    meta_config = file_path+'.json'
    try:
        with open_exclusive(meta_config, "r") as f:
            #load meta data
            meta_data = json.load(f)
            
            dtype = meta_data['dtype']
            shape = tuple(meta_data['shape'])
            
            data = np.memmap(file_path, dtype=dtype, mode='r', shape=shape)
    except Exception as e:
        logger.warning("Failed to load numpy file %s: %s", file_path, e)
    
    return data

def save_numpy(data: Any, file_path: str) -> bool:
    if isinstance(data, np.ndarray):
        with open_exclusive(file_path, "wb") as f:
            np.save(f, data)
            f.flush()
            os.fsync(f.fileno())
            logger.info(f"Saved numpy file {file_path} size {data.nbytes} human readable {np.round(data.nbytes/1024/1024,2)}MB success")
            return True
    else:
        logger.warning(f"Data is not numpy array or memmap array {type(data)} {file_path}")
        return False
    
    
    

def load_numpy(file_path: str,mmap_mode) -> np.ndarray:
    data = None
    try:
        with open_shared(file_path, "rb") as f:
            pass
            # data = np.load(f)      
        # data = np.load(file_path, mmap_mode='c')
        data = np.load(file_path, mmap_mode=mmap_mode)
    except Exception as e:
        logger.warning("Failed to load numpy file %s: %s", file_path, e)
    
    return data



def pickle_big_data(data: Any, file_path: str,rename_np_memmap_file:bool) -> None:
    """Write a pickled Python object to a file in chunks"""
    if isinstance(data, np.memmap) and rename_np_memmap_file:
        old_file_path = data.filename
        dtype = data.dtype
        shape = data.shape
        #close the memmap file
        # data._mmap.close()
        
        # rename_np_memmap(old_file_path, file_path,dtype=dtype,shape=shape)
        return
    
    if isinstance(data, np.ndarray):
        save_numpy(data, file_path)
        return

    bytes_out = pickle.dumps(data, protocol=4)
    with open_exclusive(file_path, "wb") as f_out:
        for idx in range(0, len(bytes_out), MAX_PICKLE_BYTES):
            f_out.write(bytes_out[idx: idx + MAX_PICKLE_BYTES])
            # flush and fsync to ensure data is written to disk
            f_out.flush()
            os.fsync(f_out.fileno())


def unpickle_big_data(file_path: str) -> Any:
    """Return a Python object from a file containing pickled data in chunks"""
    try:
        if file_path.endswith('.npy'):
            meta_config = file_path+'.json'
            if os.path.exists(meta_config):
                return load_np_memmap(file_path)
            else:
                return load_numpy(file_path,mmap_mode='c')
        with open_shared(file_path, "rb") as f:
            return pickle.load(f)
    except Exception:  # noqa, pylint: disable=broad-except
        logger.warning("Failed to unpickle %s", file_path)
        if file_path.endswith('.npy'):
            return None
        bytes_in = bytearray(0)
        input_size = os.path.getsize(file_path)
        with open_shared(file_path, mode="rb") as f_in:
            for _ in range(0, input_size, MAX_PICKLE_BYTES):
                bytes_in += f_in.read(MAX_PICKLE_BYTES)
        return pickle.loads(bytes_in)



def get_age_of_file(filename: str, unit: str = "days") -> int:
    """Return relative age of a file as a datetime.timedelta"""
    age = datetime.today() - datetime.fromtimestamp(getmtime(filename))
    return getattr(age, unit)


def get_files_in_directory(directory: str) -> List[str]:
    """Return all files in a directory, non-recursive"""
    return [f for f in os.listdir(directory) if isfile(join_path(directory, f))]


def delete_old_disk_caches() -> None:
    cache_metadata = load_cache_metadata_json()
    new_cache_metadata = deepcopy(cache_metadata)
    cache_changed = False
    for function_name, function_caches in cache_metadata.items():
        if function_name == _TOTAL_NUMCACHE_KEY:
            continue
        to_keep = []
        for function_cache in function_caches:
            max_age_days = int(function_cache["max_age_days"])
            file_name = join_path(DISK_CACHE_DIR, function_cache["file_name"])
            if not file_exists(file_name):
                cache_changed = True
                continue
            if not get_age_of_file(file_name) > max_age_days != UNLIMITED_CACHE_AGE:
                to_keep.append(function_cache)
                continue
            logger.info(
                "Removing stale cache file %s, > %d days", file_name, max_age_days
            )
            cache_changed = True
            os.remove(file_name)
        if to_keep:
            new_cache_metadata[function_name] = to_keep
    if cache_changed:
        write_cache_file(new_cache_metadata)


def get_disk_cache_for_function(function_name: str) -> Optional[Dict]:
    cache_metadata = load_cache_metadata_json()
    return cache_metadata.get(function_name, None)


def get_disk_cache_size_for_function(function_name: str) -> Optional[int]:
    """Return the current number of entries in the cache for a function by name"""
    function_cache = get_disk_cache_for_function(function_name)
    return None if function_cache is None else len(function_cache)


def delete_disk_caches_for_function(function_name: str) -> None:
    logger.debug("Removing cache entries for %s", function_name)
    n_deleted = 0
    cache_metadata = load_cache_metadata_json()
    if function_name not in cache_metadata:
        return

    functions_to_delete_cache_for = cache_metadata.pop(function_name)
    for function_cache in functions_to_delete_cache_for:
        file_name = join_path(DISK_CACHE_DIR, function_cache["file_name"])
        if os.path.exists(file_name):
            os.remove(file_name)
        config_file = file_name+'.json'
        if os.path.exists(config_file):
            os.remove(config_file)
            
        config_file = file_name+'_timestamp'
        if os.path.exists(config_file):
            os.remove(config_file)
        
        n_deleted += 1
    logger.debug("Removed %s cache entries for %s", n_deleted, function_name)
    write_cache_file(cache_metadata)


def cache_exists(
    cache_metadata: Dict, function_name: str,except_arg_names: List[str] = [], *args, **kwargs
) -> Tuple[bool, Any]:
    
    if len(except_arg_names) >0:
        assert len(args) == 0, f"except_arg_names:{except_arg_names} ,args should be empty args {args}"
    for arg_name in except_arg_names:
        if arg_name in kwargs:
            kwargs.pop(arg_name)
                
    if function_name not in cache_metadata:
        logger.info(f"Function {function_name} not in cache metadata")
        return False, None
    new_caches_for_function = []
    cache_changed = False
    
    # is_npy = isinstance(function_value, np.ndarray)
    # post_fix = ".npy" if is_npy else ".pkl"
    # new_file_name = str(int(cache_metadata[_TOTAL_NUMCACHE_KEY]) + 1) + post_fix
    new_file_name = get_hash_filename(function_name,str(args),str(kwargs))
    new_file_name = new_file_name
    file_path_npy = join_path(DISK_CACHE_DIR, new_file_name+'.npy')
    file_path_pkl = join_path(DISK_CACHE_DIR, new_file_name+'.pkl')
    is_npy = file_exists(file_path_npy)
    is_pkl = file_exists(file_path_pkl)
    if is_npy:
        file_name = file_path_npy
    elif is_pkl:
        file_name = file_path_pkl
    else:
        logger.info(f"Function {function_name} cache file {new_file_name} not found args:{str(args)} kwargs:{str(kwargs)}")
        return False, None
    max_age_days = UNLIMITED_CACHE_AGE
    for function_cache in cache_metadata[function_name]:
        if function_cache["args"] == str(args) and (function_cache["kwargs"] == str(kwargs)):
            max_age_days = int(function_cache["max_age_days"])
            break

    if get_age_of_file(file_name) > max_age_days != UNLIMITED_CACHE_AGE:
        logger.info(f"Cache file {file_name} is stale, removing args {args} kwargs {kwargs}")
        os.remove(file_name)
        cache_changed = True
    else:
        function_value = unpickle_big_data(file_name)
        if function_value is not None:
            return True, function_value
        else:
            logger.warning(f"Failed to load cache file {file_name} args {args} kwargs {kwargs}")
            return False, None
    return False, None


def cache_exists2(
    cache_metadata: Dict, function_name: str, *args, **kwargs
) -> Tuple[bool, Any]:
    if function_name not in cache_metadata:
        return False, None
    new_caches_for_function = []
    cache_changed = False
    for function_cache in cache_metadata[function_name]:
        if function_cache["args"] == str(args) and (
            function_cache["kwargs"] == str(kwargs)
        ):
            max_age_days = int(function_cache["max_age_days"])
            file_name = join_path(DISK_CACHE_DIR, function_cache["file_name"])
            if file_exists(file_name):
                if get_age_of_file(file_name) > max_age_days != UNLIMITED_CACHE_AGE:
                    logger.info(f"Cache file {file_name} is stale, removing args {args} kwargs {kwargs}")
                    os.remove(file_name)
                    cache_changed = True
                else:
                    function_value = unpickle_big_data(file_name)
                    if function_value is not None:
                        return True, function_value
                    else:
                        logger.warning(f"Failed to load cache file {file_name} args {args} kwargs {kwargs}")
                        return False, None
                        # os.remove(file_name)
                        # cache_changed = True
            else:
                logger.info(f"Cache file {file_name} does not exist args {args} kwargs {kwargs}")
                cache_changed = True
        else:
            # logger.info(f"Cache file {function_cache['file_name']} args {args} kwargs {kwargs} does not match")
            new_caches_for_function.append(function_cache)
    if cache_changed:
        if new_caches_for_function:
            cache_metadata[function_name] = new_caches_for_function
        else:
            cache_metadata.pop(function_name)
        write_cache_file(cache_metadata)
    return False, None

def cache_exists_for_function(function_name: str,except_arg_names,*args, **kwargs)-> Tuple[bool, Any]:
    cache_metadata = load_cache_metadata_json()
    
    return cache_exists(cache_metadata,function_name,except_arg_names,*args, **kwargs)

def cache_exists_rename_to_hash():
    cache_metadata = load_cache_metadata_json()
    for function_name, function_caches in cache_metadata.items():
        if function_name == _TOTAL_NUMCACHE_KEY:
            continue
        to_keep = []
        for function_cache in function_caches:
            max_age_days = int(function_cache["max_age_days"])
            old_filename = function_cache["file_name"]
            file_postfix = old_filename.split('.')[-1]
            hash_filename = get_hash_filename(function_name,function_cache["args"],function_cache["kwargs"])
            hash_filename = hash_filename + '.' + file_postfix
            
            if old_filename != hash_filename:
                logger.info(f"Renaming cache file {old_filename} to {hash_filename}")
                os.rename(join_path(DISK_CACHE_DIR, old_filename),join_path(DISK_CACHE_DIR, hash_filename))
                logger.info(f"Renamed cache file {old_filename} to {hash_filename}")
                function_cache["file_name"] = hash_filename
            to_keep.append(function_cache)
        cache_metadata[function_name] = to_keep
    
    write_cache_file(cache_metadata)
    

import hashlib

def get_hash_filename(function_name,*args,**kwargs):
    # Generate a unique, human-readable filename based on input parameters
    filename = function_name+'_'+str(args)+'_'+str(kwargs)

    hash_str = hashlib.sha1(str(filename).encode()).hexdigest()
    filename = f"{function_name}_{hash_str}"
    return filename

def cache_function_value(
    function_value: Any,
    n_days_to_cache: int,
    cache_metadata: Any,
    rename_np_memmap_file:bool,
    function_name: str,
    *args,
    **kwargs,
) -> None:
    if function_name == _TOTAL_NUMCACHE_KEY:
        raise Exception("Cant cache function named %s" % _TOTAL_NUMCACHE_KEY)
    

    is_npy = isinstance(function_value, np.ndarray)
    post_fix = ".npy" if is_npy else ".pkl"
    # new_file_name = str(int(cache_metadata[_TOTAL_NUMCACHE_KEY]) + 1) + post_fix
    new_file_name = get_hash_filename(function_name,str(args),str(kwargs))
    new_file_name = new_file_name + post_fix
    new_cache = {
        "args": str(args),
        "kwargs": str(kwargs),
        "file_name": new_file_name,
        "max_age_days": n_days_to_cache,
    }
    new_filepath = join_path(DISK_CACHE_DIR, new_file_name)
    pickle_big_data(function_value, new_filepath,rename_np_memmap_file)
    
    # with open_shared(DISK_CACHE_FILE,mode='r+') as f:
    #     cache_metadata = json.load(f)
    function_caches = cache_metadata.get(function_name, [])
    function_caches.append(new_cache)
    cache_metadata[function_name] = function_caches
    cache_metadata[_TOTAL_NUMCACHE_KEY] = int(cache_metadata[_TOTAL_NUMCACHE_KEY]) + 1
        # json.dump(cache_metadata, f)
    write_cache_file(cache_metadata)
    return new_filepath

# F = TypeVar("F", bound=Callable[..., Any])


def cache_to_disk(n_days_to_cache: int = DEFAULT_CACHE_AGE,except_arg_names: List[str] = [],rename_np_memmap_file=True) -> Callable:
    """Cache to disk"""
    if n_days_to_cache == UNLIMITED_CACHE_AGE:
        warnings.warn("Using an unlimited age cache is not recommended", stacklevel=3)
    if isinstance(n_days_to_cache, int):
        if n_days_to_cache < 0:
            n_days_to_cache = 0
    elif n_days_to_cache is not None:
        raise TypeError("Expected n_days_to_cache to be an integer or None")

    def decorating_function(original_function: Callable) -> Callable:
        wrapper = _cache_to_disk_wrapper(original_function, n_days_to_cache, _CacheInfo,except_arg_names,rename_np_memmap_file)
        return wrapper

    return decorating_function


def _cache_to_disk_wrapper(
    original_func: Callable, n_days_to_cache: int, _CacheInfo: type,except_arg_names: List[str] = [],rename_np_memmap_file=True
) -> Callable:  # pylint: disable=invalid-name
    hits = misses = nocache = 0

    def wrapper(*args, **kwargs) -> Any:
        nonlocal hits, misses, nocache 
        cache_metadata = load_cache_metadata_json()
        already_cached, function_value = cache_exists(
            cache_metadata, original_func.__name__,except_arg_names, *args, **kwargs
        )
        if already_cached:
            logger.debug(
                "Cache HIT on %s (hits=%s, misses=%s, nocache=%s)",
                original_func.__name__,
                hits,
                misses,
                nocache,
            )
            hits += 1
            return function_value

        logger.debug(
            "Cache MISS on %s (hits=%s, misses=%s, nocache=%s)",
            original_func.__name__,
            hits,
            misses,
            nocache,
        )
        logger.debug(" -- MISS ARGS:    (%s)", ",".join([str(arg) for arg in args]))
        logger.debug(
            " -- MISS KWARGS:  (%s)",
            ",".join(["{}={}".format(str(k), str(v)) for k, v in kwargs.items()]),
        )
        misses += 1

        try:
            function_value = original_func(*args, **kwargs)
        except NoCacheCondition as err:
            nocache += 1
            logger.debug(
                "%s() threw NoCacheCondition exception; no new cache entry",
                original_func.__name__,
            )
            function_value = err.function_value
        else:
            logger.debug("%s() returned, adding cache entry", original_func.__name__)
            
            if len(except_arg_names) >0:
                assert len(args) == 0, f"except_arg_names:{except_arg_names} ,args should be empty args {args}"
                logger.info(f"Removing except_arg_names {except_arg_names} from kwargs {kwargs.keys()}")
            
            for arg_name in except_arg_names:
                if arg_name in kwargs:
                    kwargs.pop(arg_name)
            cache_metadata = load_cache_metadata_json()
            new_filepath = cache_function_value(
                function_value,
                n_days_to_cache,
                cache_metadata, # Function Reentrancy
                rename_np_memmap_file,
                original_func.__name__,
                *args,
                **kwargs,
            )
        
            if isinstance(function_value, np.memmap)and rename_np_memmap_file:
                old_file_path = function_value.filename
                dtype = function_value.dtype
                shape = function_value.shape
                #close the memmap file
                # del function_value 
                
                # function_value = 
                function_value = rename_np_memmap(old_file_path, new_filepath,dtype=dtype,shape=shape)
        return function_value

    def cache_info() -> Type:
        """Report runtime cache statistics"""
        return _CacheInfo(hits, misses, nocache)

    def cache_clear() -> None:
        """Clear the cache permanently from disk for this function"""
        logger.info(
            "Cache clear requested for %s(); %s items in cache ...",
            original_func.__name__,
        )
        delete_disk_caches_for_function(original_func.__name__)

    def cache_size() -> Optional[int]:
        """Return the number of cached entries for this function"""
        return get_disk_cache_size_for_function(original_func.__name__)

    def cache_get_raw() -> Optional[Any]:
        """Return the raw cache object for this function as a list of dicts"""
        warnings.warn(
            "This is an internal interface and should not be used lightly", stacklevel=3
        )
        return get_disk_cache_for_function(original_func.__name__)

    wrapper.cache_info = cache_info  # type: ignore
    wrapper.cache_clear = cache_clear  # type: ignore
    wrapper.cache_size = cache_size  # type: ignore
    wrapper.cache_get_raw = cache_get_raw  # type: ignore
    return wrapper


ensure_dir(DISK_CACHE_DIR)
delete_old_disk_caches()

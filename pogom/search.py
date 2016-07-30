#!/usr/bin/python
# -*- coding: utf-8 -*-

'''
Search Architecture:
 - Create a Queue
   - Holds a list of locations to scan
 - Create N search threads
   - Each search thread will be responsible for hitting the API for a given scan location
 - Create a "overseer" loop
   - Creates/updates the search grid, populates the Queue, and waits for the current search iteration to complete
   -
'''

import logging
import time
import math
import threading
from itertools import cycle

from threading import Thread, Lock
from queue import Queue

from pgoapi import PGoApi
from pgoapi.utilities import f2i, get_cellid

from . import config
from .models import parse_map

log = logging.getLogger(__name__)

TIMESTAMP = '\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000'

search_queue = Queue()
accounts = None
accounts_iter = None


def calculate_lng_degrees(lat):
    return float(lng_gap_meters) / \
        (meters_per_degree * math.cos(math.radians(lat)))


def send_map_request(api, position):
    try:
        api_copy = api.copy()
        api_copy.set_position(*position)
        api_copy.get_map_objects(latitude=f2i(position[0]),
                                 longitude=f2i(position[1]),
                                 since_timestamp_ms=TIMESTAMP,
                                 cell_id=get_cellid(position[0], position[1]))
        return api_copy.call()
    except Exception as e:
        log.warning("Uncaught exception when downloading map " + str(e))
        return False

def get_new_coords(init_loc, distance, bearing):
    """ Given an initial lat/lng, a distance(in kms), and a bearing (degrees),
    this will calculate the resulting lat/lng coordinates.
    """ 
    R = 6378.1 #km radius of the earth
    bearing = math.radians(bearing)

    init_coords = [math.radians(init_loc[0]), math.radians(init_loc[1])] # convert lat/lng to radians

    new_lat = math.asin( math.sin(init_coords[0])*math.cos(distance/R) +
        math.cos(init_coords[0])*math.sin(distance/R)*math.cos(bearing))

    new_lon = init_coords[1] + math.atan2(math.sin(bearing)*math.sin(distance/R)*math.cos(init_coords[0]),
        math.cos(distance/R)-math.sin(init_coords[0])*math.sin(new_lat))

    return [math.degrees(new_lat), math.degrees(new_lon)]

def generate_location_steps(initial_loc, step_count):
    #Bearing (degrees)
    NORTH = 0
    EAST = 90
    SOUTH = 180
    WEST = 270

    pulse_radius = 0.07                 # km - radius of players heartbeat is 70m
    xdist = math.sqrt(3)*pulse_radius   # dist between column centers
    ydist = 3*(pulse_radius/2)          # dist between row centers

    yield (initial_loc[0], initial_loc[1], 0) #insert initial location

    ring = 1            
    loc = initial_loc
    while ring < step_count:
        #Set loc to start at top left
        loc = get_new_coords(loc, ydist, NORTH)
        loc = get_new_coords(loc, xdist/2, WEST)
        for direction in range(6):
            for i in range(ring):
                if direction == 0: # RIGHT
                    loc = get_new_coords(loc, xdist, EAST)
                if direction == 1: # DOWN + RIGHT
                    loc = get_new_coords(loc, ydist, SOUTH)
                    loc = get_new_coords(loc, xdist/2, EAST)
                if direction == 2: # DOWN + LEFT
                    loc = get_new_coords(loc, ydist, SOUTH)
                    loc = get_new_coords(loc, xdist/2, WEST)
                if direction == 3: # LEFT
                    loc = get_new_coords(loc, xdist, WEST)
                if direction == 4: # UP + LEFT
                    loc = get_new_coords(loc, ydist, NORTH)
                    loc = get_new_coords(loc, xdist/2, WEST)
                if direction == 5: # UP + RIGHT
                    loc = get_new_coords(loc, ydist, NORTH)
                    loc = get_new_coords(loc, xdist/2, EAST)
                yield (loc[0], loc[1], 0)
        ring += 1


def login(account, login_delay, position):
    log.info('Attempting login to Pokemon Go.')
    api = account['api']
    api.set_position(*position)
    while not api.login(account['auth_service'], account['username'], account['password']):
        log.error('Failed to login to Pokemon Go with {:s}. Trying again in {:g} seconds.'.format(account['username'], login_delay))
        time.sleep(login_delay)

    log.info('Login to Pokemon Go successful.')


#
# Search Threads Logic
#
def create_search_threads(num):
    search_threads = []
    for i in range(num):
        t = Thread(target=search_thread, name='search_thread-{}'.format(i), args=(search_queue,))
        t.daemon = True
        t.start()
        search_threads.append(t)


def search_thread(q):
    threadname = threading.currentThread().getName()
    log.debug("Search thread {}: started and waiting".format(threadname))
    while True:

        # Get the next item off the queue (this blocks till there is something)
        i, step_location, step, lock, login_delay = q.get()
        account = next(accounts_iter)
        api = account['api']

        # If a new location has been set, just mark done and continue
        if 'NEXT_LOCATION' in config:
            log.debug("{}: new location waiting, flushing queue".format(threadname))
            q.task_done()
            continue

        log.debug("{}: processing iteration {} step {}".format(threadname, i, step))
        response_dict = {}
        failed_consecutive = 0
        while not response_dict:
            response_dict = send_map_request(api, step_location)
            if response_dict:
                try:
                    if lock is None:
                        parse_map(response_dict, step_location)
                    else:
                        with lock:
                            parse_map(response_dict, step_location)
                    log.debug("{}: iteration {} step {} complete".format(threadname, i, step))
                except Exception as e:
                    log.error('Search thread failed. {0.__class__.__name__}: {0}'.format(e))
                    failed_consecutive += 1
                    if(failed_consecutive >= config['REQ_MAX_FAILED']):
                        log.error('Niantic servers under heavy load. Waiting before trying again')
                        time.sleep(config['REQ_HEAVY_SLEEP'])
                        failed_consecutive = 0
                    response_dict = None
            else:
                log.info('Map download failed, waiting and retrying')
                log.debug('{}: iteration {} step {} failed'.format(threadname, i, step))
                time.sleep(config['REQ_SLEEP'])
        
        time.sleep(config['REQ_SLEEP'])
        q.task_done()


#
# Search Overseer
#
def search_loop(args):
    i = 0
    global accounts
    global accounts_iter
    accounts = []

    if args.accounts:
        for account in args.accounts:
            account['api'] = PGoApi()
            accounts.append(account)
    else:
        accounts.append({'auth_service':args.auth_service, 'username':args.username, 'password':args.password, 'api':PGoApi()})

    accounts_iter = cycle(accounts)

    while True:
        log.info("Search loop {} starting".format(i))
        try:
            search(args, i)
            log.info("Search loop {} complete.".format(i))
            i += 1
        except Exception as e:
            log.error('Scanning error @ {0.__class__.__name__}: {0}'.format(e))
        finally:
            if args.thread_delay > 0:
                log.info('Waiting {:g} seconds before beginning new scan.'.format(args.thread_delay))
                time.sleep(args.thread_delay)


#
# Overseer main logic
#
def search(args, i):
    num_steps = args.step_limit

    # Update the location if needed
    if 'NEXT_LOCATION' in config:
        log.info('New location set')
        config['ORIGINAL_LATITUDE'] = config['NEXT_LOCATION']['lat']
        config['ORIGINAL_LONGITUDE'] = config['NEXT_LOCATION']['lon']
        config.pop('NEXT_LOCATION', None)

    position = (config['ORIGINAL_LATITUDE'], config['ORIGINAL_LONGITUDE'], 0)

    for account in accounts:
        api = account['api']
        if api._auth_provider and api._auth_provider._ticket_expire:
            remaining_time = api._auth_provider._ticket_expire/1000 - time.time()

            if remaining_time <= 120:
                login(account, args.login_delay, position)
        else:
            login(account, args.login_delay, position)

    lock = Lock() if (args.db_type == 'sqlite') else None

    for step, step_location in enumerate(generate_location_steps(position, num_steps), 1):
        log.debug("Queue search iteration {}, step {}".format(i, step))
        search_args = (i, step_location, step, lock, args.login_delay)
        search_queue.put(search_args)

    # Wait until this scan iteration queue is empty (not nessearily done)
    while not search_queue.empty():
        log.debug("Waiting for current search queue to complete (remaining: {})".format(search_queue.qsize()))
        time.sleep(1)

    # Don't let this method exit until the last item has ACTUALLY finished
    search_queue.join()


#
# A fake search loop which does....nothing!
#
def fake_search_loop():
    while True:
        log.info('Fake search loop running...')
        time.sleep(10)

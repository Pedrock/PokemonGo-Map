#!/usr/bin/python
# -*- coding: utf-8 -*-

import logging
import traceback
import time
import math
from Queue import Queue
import threading

from pgoapi import PGoApi
from pgoapi.utilities import f2i, get_cellid

from . import config
from .models import parse_map

log = logging.getLogger(__name__)

TIMESTAMP = '\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000\000'
REQ_SLEEP = 1
failed_consecutive = 0
api = PGoApi()


def send_map_request(api, position):
    try:
        api.set_position(*position)
        api.get_map_objects(latitude=f2i(position[0]),
                            longitude=f2i(position[1]),
                            since_timestamp_ms=TIMESTAMP,
                            cell_id=get_cellid(position[0], position[1]))
        return api.call()
    except Exception as e:
        log.warn("Uncaught exception when downloading map "+ e)
        return False


def hex_transform(x,y,r,il):
    return (x+y/2)*r[0]+il[0],(0.886*y)*r[1]+il[1],0

def generate_location_steps(il, num_steps):
    pos, x, y, m = 1, 0., 0., 173.2
    conv = float(111111)                            # ~meters per degree
    r = m/conv, m/conv / math.cos(il[0]*0.0174533)  # Conversion of radius from meters to deg
    yield hex_transform(x,y,r,il)
    for n in range(1,num_steps):
        n+=1
        for i in range(1, n):
            x+=1
            yield hex_transform(x,y,r,il)
        for i in range(1, n-1):
            y+=1
            yield hex_transform(x,y,r,il)
        for i in range(1, n):
            x-=1
            y+=1
            yield hex_transform(x,y,r,il)
        for i in range(1, n):
            x-=1
            yield hex_transform(x,y,r,il)
        for i in range(1, n):
            y-=1
            yield hex_transform(x,y,r,il)
        for i in range(1, n):
            x+=1
            y-=1
            yield hex_transform(x,y,r,il)
    for i in range(1, num_steps):
        x+=1
        yield hex_transform(x,y,r,il)


def login(args, position):
    log.info('Attempting login to Pokemon Go.')

    api.set_position(*position)

    while not api.login(args.auth_service, args.username, args.password):
        log.info('Failed to login to Pokemon Go. Trying again.')
        time.sleep(REQ_SLEEP)

    log.info('Login to Pokemon Go successful.')

class ThreadScan(threading.Thread):
    def __init__(self, queue, num_steps):
        threading.Thread.__init__(self)
        self.queue = queue
        self.num_steps = num_steps

    def run(self):
        while True:
            task = self.queue.get()

            step_location = task['step_location']
            i = task['i']
            num_steps = self.num_steps
            
            log.info('Scanning step {:d} of {:d}.'.format(i, 3*num_steps**2-3*num_steps))
            log.debug('Scan location is {:f}, {:f}'.format(step_location[0], step_location[1]))

            while True:
                try:
                    response_dict = send_map_request(api, step_location)
                except:
                    traceback.print_exc()
                    response_dict = False
                if response_dict:
                    try:
                        parse_map(response_dict, step_location)
                        break
                    except:
                        traceback.print_exc()
                        log.error('Scan step failed ({:d}). Trying again.'.format(i))
                else:
                    log.info('Map Download failed ({:d}). Trying again.'.format(i))

            #signals to queue job is done
            log.info('Completed {:5.2f}% of scan.'.format(float(i) / (3*num_steps**2-3*num_steps)*100))
            self.queue.task_done()


def search(args,queue):
    num_steps = args.step_limit
    position = (config['ORIGINAL_LATITUDE'], config['ORIGINAL_LONGITUDE'], 0)

    if api._auth_provider and api._auth_provider._ticket_expire:
        remaining_time = api._auth_provider._ticket_expire/1000 - time.time()

        if remaining_time > 60:
            log.info("Skipping Pokemon Go login process since already logged in for another {:.2f} seconds".format(remaining_time))
        else:
            login(args, position)
    else:
        login(args, position)

    i = 1
    for step_location in generate_location_steps(position, num_steps):
        task = {'i':i, 'step_location':step_location}
        queue.put(task)
        i += 1

def search_loop(args):
    queue = Queue()
    for i in range(25):
        t = ThreadScan(queue, args.step_limit)
        t.setDaemon(True)
        t.start()
    while True:
        search(args,queue)
        queue.join()
        log.info("Scanning complete.")
        if args.scan_delay > 1:
            log.info('Waiting {:d} seconds before beginning new scan.'.format(args.scan_delay))
        time.sleep(args.scan_delay)

#!/usr/bin/python

'''Channel Access Example'''

# load correct version of catools
import require
from cothread.catools import *

print caget('SR21C-DI-DCCT-01:SIGNAL')

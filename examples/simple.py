#!/bin/env dls-python2.6

'''Channel Access Example'''

from __future__ import print_function

# load correct version of catools
import require
from cothread.catools import *

print(caget('SR21C-DI-DCCT-01:SIGNAL'))

#!/usr/bin/python
import os
import sys	
from subprocess import Popen

virtenv = os.path.join(os.environ.get('OPENSHIFT_PYTHON_DIR','.'), 'virtenv')
virtualenv = os.path.join(virtenv, 'bin/activate_this.py')
try:
    execfile(virtualenv, dict(__file__=virtualenv))
except IOError:
    pass
#
# IMPORTANT: Put any additional includes below this line.  If placed above this
# line, it's possible required libraries won't be in your searchable path
#

if __name__ == '__main__':
    a = Popen([sys.executable, 'runserver.py'])
    b = Popen([sys.executable, 'runserver.py', '--no-server', '--location', 'LOCATION', '-u', 'USER', '-p', 'PASS'])
    a.wait()
    b.wait()
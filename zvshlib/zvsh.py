#  Copyright 2014 Rackspace, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

try:
    import configparser as ConfigParser
except ImportError:
    # Python 2 fallback
    import ConfigParser
import argparse
import array
import fcntl
import os
import re
import shutil
import stat
import sys
import tarfile
import termios
from pty import _read as pty_read
from pty import _copy as pty_copy
import pty
import threading
import tty

from os import path
from subprocess import Popen, PIPE
from tempfile import mkdtemp


ENV_MATCH = re.compile(r'([_A-Z0-9]+)=(.*)')
DEFAULT_MANIFEST = {
    'Version': '20130611',
    'Memory': '%d' % (4 * 1024 * 1024 * 1024),
    'Node': 1,
    'Timeout': 50
}
DEFAULT_LIMITS = {
    'reads': str(1024 * 1024 * 1024 * 4),
    'rbytes': str(1024 * 1024 * 1024 * 4),
    'writes': str(1024 * 1024 * 1024 * 4),
    'wbytes': str(1024 * 1024 * 1024 * 4)
}
CHANNEL_SEQ_READ_TEMPLATE = 'Channel = %s,%s,0,0,%s,%s,0,0'
CHANNEL_SEQ_WRITE_TEMPLATE = 'Channel = %s,%s,0,0,0,0,%s,%s'
CHANNEL_RANDOM_RW_TEMPLATE = 'Channel = %s,%s,3,0,%s,%s,%s,%s'
CHANNEL_RANDOM_RO_TEMPLATE = 'Channel = %s,%s,3,0,%s,%s,0,0'

DEBUG_TEMPLATE = '''set confirm off
b CreateSession
r
b main
add-symbol-file %s 0x440a00020000
shell clear
c
d br
'''

CHANNEL_TEMPLATE = 'Channel = %s'

MANIFEST_TEMPLATE = """\
Node = %(node)s
Version = %(version)s
Timeout = %(timeout)s
Memory = %(memory)s
Program = %(program)s
%(channels)s"""

MANIFEST_DEFAULTS = dict(
    version='20130611',
    memory=4294967296,
    node=1,
    timeout=50,
)

GETS_DEFAULT = 4294967296
GET_SIZE_DEFAULT_BYTES = 4294967296
PUTS_DEFAULT = 4294967296
PUT_SIZE_DEFAULT_BYTES = 4294967296

SEQ_READ_SEQ_WRITE = 0
RND_READ_SEQ_WRITE = 1
SEQ_READ_RND_WRITE = 2
RND_READ_RND_WRITE = 3


class Channel(object):
    """
    Definition of a channel within a manifest. Defines a mapping from the host
    to the ZeroVM filesystem, access type, and read/write limits.

    :param uri:
        Path to a local file, pipe, character device, tcp socket or host ID.
    :param alias:
        Path where this channel will be mounted in ZeroVM.
    :param access_type:
        Choose from the following:

            * 0: sequential read/ sequential write
            * 1: random read/ sequential write
            * 2: sequential read / random write
            * 3: random read / random write
    :param etag:
        etag switch; can be in the range 0..1

        Default: 0
    :param gets:
        Limit for number of reads from this channel.

        Default: 4294967296
    :param get_size:
        Limit on total amount of data to read from this channel, in bytes.

        Default: 4294967296
    :param puts:
        Limit for number of writes to this channel.

        Default: 4294967296
    :param put_size:
        Limit on total amount of data to be written to this channel, in bytes.

        Default: 4294967296
    """

    def __init__(self, uri, alias, access_type,
                 etag=0,
                 gets=GETS_DEFAULT,
                 get_size=GET_SIZE_DEFAULT_BYTES,
                 puts=PUTS_DEFAULT,
                 put_size=PUT_SIZE_DEFAULT_BYTES):
        self.uri = uri
        self.alias = alias
        self.access_type = access_type
        self.etag = etag
        self.gets = gets
        self.get_size = get_size
        self.puts = puts
        self.put_size = put_size

    def __str__(self):
        return 'Channel = %s,%s,%s,%s,%s,%s,%s,%s' % (
            self.uri, self.alias, self.access_type, self.etag,
            self.gets, self.get_size, self.puts, self.put_size
        )


class Manifest(object):
    """
    Object representation of a ZeroVM manifest. Includes utilities and sane
    defaults for generating manifest files.
    """
    DEFAULT_NODE = 1

    def __init__(self, version, timeout, memory, program, node=DEFAULT_NODE,
                 etag=0, channels=None):
        self.version = version
        self.timeout = timeout
        self.memory = memory
        self.program = program

        self.node = node
        self.etag = etag

        self.channels = channels
        if self.channels is None:
            self.channels = []

    @classmethod
    def default_manifest(cls, basedir, program):
        channels = [
            Channel('/dev/stdin', '/dev/stdin', SEQ_READ_SEQ_WRITE, puts=0,
                    put_size=0),
            Channel(path.join(basedir, 'stdout.%s' % cls.DEFAULT_NODE),
                    '/dev/stdout', SEQ_READ_SEQ_WRITE, gets=0, get_size=0),
            Channel(path.join(basedir, 'stderr.%s' % cls.DEFAULT_NODE),
                    '/dev/stderr', SEQ_READ_SEQ_WRITE, gets=0, get_size=0),
            Channel(path.join(basedir, 'nvram.%s' % cls.DEFAULT_NODE),
                    '/dev/nvram', RND_READ_RND_WRITE),
        ]
        return Manifest(MANIFEST_DEFAULTS['version'],
                        MANIFEST_DEFAULTS['timeout'],
                        MANIFEST_DEFAULTS['memory'],
                        program,
                        channels=channels)

    def dumps(self):
        """
        Get the text representation of the manifest.
        """
        if not self.channels:
            raise RuntimeError("Manifest must have at least 1 channel.")

        manifest = MANIFEST_TEMPLATE
        manifest %= dict(
            node=self.node,
            version=self.version,
            timeout=self.timeout,
            memory='%s,%s' % (self.memory, self.etag),
            program=self.program,
            channels='\n'.join([str(c) for c in self.channels]),
        )
        return manifest

    def dump(self, fp):
        fp.write(self.dumps())


class NVRAM(object):
    """
    Object representation of a ZeroVM nvram file.
    """

    def __init__(self, cmdline_args, ):
        pass


def run_zerovm(zvconfig, zvargs):
    """
    :param zvconfig:
        :class:`ZvConfig` instance.
    :param zvargs:
        :class:`ZvArgs` instance.
    """
    if zvargs.args.zvm_save_dir is None:
        # use a temp dir
        working_dir = mkdtemp()
    else:
        # use the specified dir
        working_dir = path.abspath(path.expanduser(zvargs.args.zvm_save_dir))

    if not path.exists(working_dir):
        os.makedirs(working_dir)

    # Manifest config options from the command line / zvsh.cfg
    man_cfg = zvconfig['manifest']
    node = man_cfg['Node']
    processed_images = list(_process_images(zvargs.args.zvm_image))

    # Search the tar images and extract the target nexe to the working
    # directory
    program_path = _extract_nexe(working_dir, node, processed_images,
                                 zvargs.args.command)

    # Write the manifest
    manifest = Manifest.default_manifest(working_dir, program_path)
    manifest.version = man_cfg['Version']
    manifest.timeout = man_cfg['Timeout']
    manifest.memory = man_cfg['Memory']
    manifest.node = node
    # TODO: add zvm_images to channels!

    for i, (zvm_image, _, _) in enumerate(
            processed_images, start=1):
        mount_point = '/dev/%s.%s' % (i, path.basename(zvm_image))

        ch = Channel(
            zvm_image, mount_point, access_type=RND_READ_RND_WRITE,
            gets=zvconfig['limits']['reads'],
            get_size=zvconfig['limits']['rbytes'],
            puts=zvconfig['limits']['writes'],
            put_size=zvconfig['limits']['wbytes'],
        )
        manifest.channels.append(ch)

    manifest_file = path.join(working_dir, 'manifest.%s' % node)
    with open(manifest_file, 'w') as man_fp:
        # TODO: check if this file exists first!
        manifest.dump(man_fp)
    # manifest_path = _write_manifest(working_dir, node, manifest)

    # create nvram file
    with open(path.join(working_dir, 'nvram.%s' % node), 'w') as nvr_fp:
        _write_nvram(zvconfig, zvargs, processed_images, nvr_fp)
    stdout = path.join(working_dir, 'stdout.%s' % node)
    stderr = path.join(working_dir, 'stderr.%s' % node)
    os.mkfifo(stdout)
    os.mkfifo(stderr)

    try:
        zvm_run = ['zerovm', '-PQ']
        if zvargs.args.zvm_trace:
            trace_log = path.abspath('zvsh.trace.log')
            zvm_run.extend(['-T', trace_log])
        zvm_run.append(manifest_file)
        runner = ZvRunner(zvm_run, stdout, stderr, working_dir,
                          getrc=zvargs.args.zvm_getrc)
        runner.run()
    finally:
        # If we're using a tempdir for the working files,
        # destroy the directory to clean up.
        if zvargs.args.zvm_save_dir is None:
            shutil.rmtree(working_dir)


def _process_images(zvm_images):
    for image in zvm_images:
        image_split = image.split(',')
        # mount_dir and access_type are optional,
        # so defaults are provided:
        mount_dir = '/'
        access_type = 'ro'

        if len(image_split) == 1:
            path = image_split[0]
        elif len(image_split) == 2:
            path, mount_dir = image_split
        elif len(image_split) == 3:
            path, mount_dir, access_type = image_split

        yield path, mount_dir, access_type


def _extract_nexe(working_dir, node, processed_images, command):
    program_path = path.join(working_dir, 'boot.%s' % node)

    with open(program_path, 'w') as program_fp:
        # TODO: check if this file already exists
        for zvm_image, _, _ in processed_images:
            try:
                tf = tarfile.open(zvm_image)
                nexe_fp = tf.extractfile(command)
                # once we've found the nexe the user wants to run,
                # we're done
                program_fp.write(nexe_fp.read())
                return program_path
            except KeyError:
                # program not found in this image,
                # go to the next and keep searching
                pass
            finally:
                tf.close()


def _write_nexe(working_dir, node, nexe_fp):
    program_path = path.join(working_dir, 'boot.%s' % node)
    with open(program_path, 'w') as program_fp:
        # TODO: check if the file already exists
        program_fp.write(nexe_fp.read())

    return program_path


def _write_manifest(working_dir, node, manifest):
    """
    :param manifest:
        :class:`Manifest` instance.
    """
    manifest_path = path.join(working_dir, 'manifest.%s' % node)
    with open(manifest_path, 'w') as manifest_fp:
        # TODO: check if file already exists
        manifest.dump(manifest_fp)

    return manifest_path


def _write_nvram(zvconfig, zvargs, processed_images, fp):

    nvram_template = """\
[args]
args = %(args)s
[fstab]
%(fstab)s
[mapping]
%(mapping)s"""
    # TODO: what about the option 'env' and 'debug' sections
    # TODO: this file isn't actually a real INI file, because
    # keys are duplicated

    fstab_channels = []
    for i, (zvm_image, mount_point, access) in enumerate(processed_images,
                                                         start=1):
        device = '/dev/%s.%s' % (i, path.basename(zvm_image))
        fstab_channel = (
            'channel=%(device)s,mountpoint=%(mount_point)s,access=%(access)s'
            ',removable=no'
            % dict(device=device, mount_point=mount_point, access=access)
        )
        fstab_channels.append(fstab_channel)

    mapping = ''
    if sys.stdin.isatty():
        mapping += 'channel=/dev/stdin,mode=char\n'
    if sys.stdout.isatty():
        mapping += 'channel=/dev/stdout,mode=char\n'
    if sys.stderr.isatty():
        mapping += 'channel=/dev/stderr,mode=char\n'

    args = [zvargs.args.command] + zvargs.args.cmd_args
    # Commas and spaces need to be properly encoded, in order for commands like
    # `python -c "print 42"` to work:
    args = ['%s' % a.replace(',', '\\x2c').replace(' ', '\\x20')
            for a in args]
    nvram_template %= dict(
        args=' '.join(args),
        fstab='\n'.join(fstab_channels),
        mapping=mapping,
    )
    fp.write(nvram_template)


class ZvArgs:
    """
    :attr args:
        :class:`argparse.Namespace` representing the command line arguments.
    """

    def __init__(self):
        self.parser = argparse.ArgumentParser(
            formatter_class=argparse.RawTextHelpFormatter
        )
        self.args = None
        self.add_agruments()

    def add_agruments(self):
        self.parser.add_argument(
            'command',
            help=('Zvsh command, can be:\n'
                  '- path to ZeroVM executable\n'
                  '- "gdb" (for running debugger)\n'),
        )
        self.parser.add_argument(
            '--zvm-image',
            help=('ZeroVM image file(s) in the following '
                  'format:\npath[,mount point][,access type]\n'
                  'defaults: path,/,ro\n'),
            action='append',
        )
        self.parser.add_argument(
            '--zvm-debug',
            help='Enable ZeroVM debug output into zvsh.log\n',
            action='store_true',
        )
        self.parser.add_argument(
            '--zvm-trace',
            help='Enable ZeroVM trace output into zvsh.trace.log\n',
            action='store_true',
        )
        self.parser.add_argument(
            '--zvm-verbosity',
            help='ZeroVM debug verbosity level\n',
            type=int,
        )
        self.parser.add_argument(
            '--zvm-getrc',
            help=('If set, zvsh will exit with '
                  'zerovm return code and not the application one\n'),
            action='store_true',
        )
        self.parser.add_argument(
            '--zvm-save-dir',
            help=('Save ZeroVM environment files into provided directory,\n'
                  'directory will be created/re-created\n'),
            action='store',
        )
        self.parser.add_argument(
            'cmd_args',
            help='command line arguments\n',
            nargs=argparse.REMAINDER,
        )

    def parse(self, zvsh_args):
        self.args = self.parser.parse_args(args=zvsh_args)


class DebugArgs(ZvArgs):

    def parse(self, zvsh_args):
        self.args = self.parser.parse_args(args=zvsh_args)
        self.args.gdb_args = []
        while self.args.cmd_args:
            arg = self.args.cmd_args.pop(0)
            if arg == '--args':
                break
            self.args.gdb_args.append(arg)
        self.args.command = self.args.cmd_args.pop(0)


class ZvConfig(ConfigParser.ConfigParser):

    def __init__(self):
        ConfigParser.ConfigParser.__init__(self)
        self.add_section('manifest')
        self.add_section('env')
        self.add_section('limits')
        self.add_section('fstab')
        self.add_section('zvapp')
        self._sections['manifest'].update(DEFAULT_MANIFEST)
        self._sections['limits'].update(DEFAULT_LIMITS)
        self.optionxform = str

    def __getitem__(self, item):
        return self._sections[item]

    def __setitem__(self, key, value):
        self._sections[key] = value


class ZvShell(object):

    def __init__(self, config, savedir=None):
        self.temp_files = []
        self.nvram_fstab = {}
        self.nvram_args = None
        self.nvram_filename = None
        self.program = None
        self.savedir = None
        self.tmpdir = None
        self.config = config
        self.savedir = savedir
        if self.savedir:
            self.tmpdir = os.path.abspath(self.savedir)
            if os.path.isdir(self.tmpdir):
                shutil.rmtree(self.tmpdir)
            os.makedirs(self.tmpdir)
        else:
            self.tmpdir = mkdtemp()
        self.node_id = self.config['manifest']['Node']
        self.config['manifest']['Memory'] += ',0'
        self.stdout = os.path.join(self.tmpdir, 'stdout.%d' % self.node_id)
        self.stderr = os.path.join(self.tmpdir, 'stderr.%d' % self.node_id)
        stdin = '/dev/stdin'
        self.channel_seq_read_template = CHANNEL_SEQ_READ_TEMPLATE \
            % ('%s', '%s', self.config['limits']['reads'],
               self.config['limits']['rbytes'])
        self.channel_seq_write_template = CHANNEL_SEQ_WRITE_TEMPLATE \
            % ('%s', '%s', self.config['limits']['writes'],
               self.config['limits']['wbytes'])
        self.channel_random_ro_template = CHANNEL_RANDOM_RO_TEMPLATE \
            % ('%s', '%s', self.config['limits']['reads'],
               self.config['limits']['rbytes'])
        self.channel_random_rw_template = CHANNEL_RANDOM_RW_TEMPLATE \
            % ('%s', '%s', self.config['limits']['reads'],
               self.config['limits']['rbytes'],
               self.config['limits']['writes'],
               self.config['limits']['wbytes'])
        self.manifest_channels = [
            self.channel_seq_read_template % (stdin, '/dev/stdin'),
            self.channel_seq_write_template % (self.stdout, '/dev/stdout'),
            self.channel_seq_write_template % (self.stderr, '/dev/stderr')
        ]
        for k, v in self.config['fstab'].iteritems():
            self.nvram_fstab[self.create_manifest_channel(k)] = v

    def create_manifest_channel(self, file_name):
        name = os.path.basename(file_name)
        self.temp_files.append(file_name)
        devname = '/dev/%s.%s' % (len(self.temp_files), name)
        abs_path = os.path.abspath(file_name)
        if not os.path.exists(abs_path):
            fd = open(abs_path, 'wb')
            fd.close()
        if os.access(abs_path, os.W_OK):
            self.manifest_channels.append(self.channel_random_rw_template
                                          % (abs_path, devname))
        else:
            self.manifest_channels.append(self.channel_random_ro_template
                                          % (abs_path, devname))
        return devname

    def add_untrusted_args(self, program, cmdline):
        self.program = program
        untrusted_args = [os.path.basename(program)]
        for arg in cmdline:
            if arg.startswith('@'):
                arg = arg[1:]
                m = ENV_MATCH.match(arg)
                if m:
                    self.config['env'][m.group(1)] = m.group(2)
                else:
                    dev_name = self.create_manifest_channel(arg)
                    untrusted_args.append(dev_name)
            else:
                untrusted_args.append(arg)

        self.nvram_args = {
            'args': untrusted_args
        }

    def add_image_args(self, zvm_image):
        if not zvm_image:
            return
        for img in zvm_image:
            (imgpath, imgmp, imgacc) = (img.split(',') + [None] * 3)[:3]
            dev_name = self.create_manifest_channel(imgpath)
            self.nvram_fstab[dev_name] = '%s %s' % (imgmp or '/',
                                                    imgacc or 'ro')
            tar = tarfile.open(name=imgpath)
            nexe = None
            try:
                nexe = tar.extractfile(self.program)
                tmpnexe_fn = os.path.join(self.tmpdir,
                                          'boot.%d' % self.node_id)
                tmpnexe_fd = open(tmpnexe_fn, 'wb')
                read_iter = iter(lambda: nexe.read(65535), '')
                for chunk in read_iter:
                    tmpnexe_fd.write(chunk)
                tmpnexe_fd.close()
                self.program = tmpnexe_fn
            except KeyError:
                pass

    def add_debug(self, zvm_debug):
        if zvm_debug:
            self.manifest_channels.append(self.channel_seq_write_template
                                          % (os.path.abspath('zvsh.log'),
                                             '/dev/debug'))

    def create_nvram(self, verbosity):
        nvram = '[args]\n'
        nvram += 'args = %s\n' % ' '.join(
            ['%s' % a.replace(',', '\\x2c').replace(' ', '\\x20')
             for a in self.nvram_args['args']])
        if len(self.config['env']) > 0:
            nvram += '[env]\n'
            for k, v in self.config['env'].iteritems():
                nvram += 'name=%s,value=%s\n' % (k, v.replace(',', '\\x2c'))
        if len(self.nvram_fstab) > 0:
            nvram += '[fstab]\n'
            for channel, mount in self.nvram_fstab.iteritems():
                (mp, access) = mount.split()
                nvram += ('channel=%s,mountpoint=%s,access=%s,removable=no\n'
                          % (channel, mp, access))
        if sys.stdin.isatty() or sys.stdout.isatty() or sys.stderr.isatty():
            nvram += '[mapping]\n'
            if sys.stdin.isatty():
                nvram += 'channel=/dev/stdin,mode=char\n'
            if sys.stdout.isatty():
                nvram += 'channel=/dev/stdout,mode=char\n'
            if sys.stderr.isatty():
                nvram += 'channel=/dev/stderr,mode=char\n'
        if verbosity:
            nvram += '[debug]\nverbosity=%d\n' % verbosity
        self.nvram_filename = os.path.join(self.tmpdir,
                                           'nvram.%d' % self.node_id)
        nvram_fd = open(self.nvram_filename, 'wb')
        nvram_fd.write(nvram)
        nvram_fd.close()

    def create_manifest(self):
        manifest = ''
        for k, v in self.config['manifest'].iteritems():
            manifest += '%s = %s\n' % (k, v)
        manifest += 'Program = %s\n' % os.path.abspath(self.program)
        self.manifest_channels.append(self.channel_random_rw_template
                                      % (os.path.abspath(self.nvram_filename),
                                         '/dev/nvram'))
        manifest += '\n'.join(self.manifest_channels)
        manifest_fn = os.path.join(self.tmpdir, 'manifest.%d' % self.node_id)
        manifest_fd = open(manifest_fn, 'wb')
        manifest_fd.write(manifest)
        manifest_fd.close()
        return manifest_fn

    def add_arguments(self, args):
        self.add_debug(args.zvm_debug)
        self.add_untrusted_args(args.command, args.cmd_args)
        self.add_image_args(args.zvm_image)
        self.create_nvram(args.zvm_verbosity)
        manifest_file = self.create_manifest()
        return manifest_file

    def cleanup(self):
        if not self.savedir:
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def add_debug_script(self):
        exec_path = os.path.abspath(self.program)
        debug_scp = DEBUG_TEMPLATE % exec_path
        debug_scp_fn = os.path.join(self.tmpdir, 'debug.scp')
        debug_scp_fd = open(debug_scp_fn, 'wb')
        debug_scp_fd.write(debug_scp)
        debug_scp_fd.close()
        return debug_scp_fn


def parse_return_code(report):
    rc = report.split('\n', 5)[2]
    try:
        rc = int(rc)
    except ValueError:
        rc = int(rc.replace('user return code = ', ''))
    return rc


class ZvRunner:

    def __init__(self, command_line, stdout, stderr, tempdir, getrc=False):
        self.command = command_line
        self.tmpdir = tempdir
        self.process = None
        self.stdout = stdout
        self.stderr = stderr
        self.getrc = getrc
        self.report = ''
        self.rc = -255
        # os.mkfifo(self.stdout)
        # os.mkfifo(self.stderr)

    def run(self):
        try:
            self.process = Popen(self.command, stdin=PIPE, stdout=PIPE)
            self.spawn(True, self.stdin_reader)
            err_reader = self.spawn(True, self.stderr_reader)
            rep_reader = self.spawn(True, self.report_reader)
            writer = self.spawn(True, self.stdout_write)
            self.process.wait()
            rep_reader.join()
            self.rc = parse_return_code(self.report)
            if self.process.returncode == 0:
                writer.join()
                err_reader.join()
        except (KeyboardInterrupt, Exception):
            pass
        finally:
            if self.process:
                self.process.wait()
                if self.process.returncode > 0:
                    self.print_error(self.process.returncode)
            rc = self.rc
            if self.getrc:
                rc = self.process.returncode
            sys.exit(rc)

    def stdin_reader(self):
        if sys.stdin.isatty():
            try:
                for l in sys.stdin:
                    self.process.stdin.write(l)
            except IOError:
                pass
        else:
            try:
                for l in iter(lambda: sys.stdin.read(65535), ''):
                    self.process.stdin.write(l)
            except IOError:
                pass
        self.process.stdin.close()

    def stderr_reader(self):
        err = open(self.stderr)
        try:
            for l in iter(lambda: err.read(65535), ''):
                sys.stderr.write(l)
        except IOError:
            pass
        err.close()

    def stdout_write(self):
        pipe = open(self.stdout)
        if sys.stdout.isatty():
            for line in pipe:
                sys.stdout.write(line)
        else:
            for line in iter(lambda: pipe.read(65535), ''):
                sys.stdout.write(line)
        pipe.close()

    def report_reader(self):
        for line in iter(lambda: self.process.stdout.read(65535), ''):
            self.report += line

    def spawn(self, daemon, func, **kwargs):
        thread = threading.Thread(target=func, kwargs=kwargs)
        thread.daemon = daemon
        thread.start()
        return thread

    def print_error(self, rc):
        for f in os.listdir(self.tmpdir):
            path = os.path.join(self.tmpdir, f)
            if stat.S_ISREG(os.stat(path).st_mode):
                if is_binary_string(open(path).read(1024)):
                    sys.stderr.write('%s is a binary file\n' % path)
                else:
                    sys.stderr.write('\n'.join(['-' * 10 + f + '-' * 10,
                                                open(path).read(), '-' * 25,
                                                '']))
        sys.stderr.write(self.report)
        sys.stderr.write("ERROR: ZeroVM return code is %d\n" % rc)


def is_binary_string(byte_string):
    textchars = ''.join(
        map(chr, [7, 8, 9, 10, 12, 13, 27] + range(0x20, 0x100))
    )
    return bool(byte_string.translate(None, textchars))


def spawn(argv, master_read=pty_read, stdin_read=pty_read):
    """Create a spawned process.
    Based on pty.spawn code."""
    # TODO(LB): This type check won't work with python3
    # See http://packages.python.org/six/#six.string_types
    # for a possible solution.
    if isinstance(argv, (basestring)):
        argv = (argv,)
    pid, master_fd = pty.fork()
    if pid == pty.CHILD:
        os.execlp(argv[0], *argv)
    try:
        mode = tty.tcgetattr(pty.STDIN_FILENO)
        tty.setraw(pty.STDIN_FILENO)
        restore = 1
    except tty.error:    # This is the same as termios.error
        restore = 0
    # get pseudo-terminal window size
    buf = array.array('h', [0, 0, 0, 0])
    fcntl.ioctl(pty.STDOUT_FILENO, termios.TIOCGWINSZ, buf, True)
    # pass window size settings to forked one
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, buf)
    try:
        pty_copy(master_fd, master_read, stdin_read)
    except (IOError, OSError):
        if restore:
            tty.tcsetattr(pty.STDIN_FILENO, tty.TCSAFLUSH, mode)

    os.close(master_fd)

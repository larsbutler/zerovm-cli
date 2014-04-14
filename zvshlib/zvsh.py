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

try:
    from collections import OrderedDict
except ImportError:
    # Python 2.6 fallback
    from ordereddict import OrderedDict

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

NVRAM_TEMPLATE = """\
[args]
args = %(args)s
[fstab]
%(fstab)s
[mapping]
%(mapping)s
"""

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

READS_DEFAULT = 4294967296
RBYTES_DEFAULT = 4294967296
WRITES_DEFAULT = 4294967296
WBYTES_DEFAULT = 4294967296

SEQ_READ_SEQ_WRITE = 0
RND_READ_SEQ_WRITE = 1
SEQ_READ_RND_WRITE = 2
RND_READ_RND_WRITE = 3

_DEFAULT_MOUNT_DIR = '/'
_DEFAULT_MOUNT_ACCESS = 'ro'


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
    :param access:
        'rw' or 'ro'. Default: 'ro'.
    :param mount_dir:
        Location where the channel should be mounted inside the ZeroVM virtual
        file system.
    :param etag:
        etag switch; can be in the range 0..1

        Default: 0
    :param reads:
        Limit for number of reads from this channel.

        Default: 4294967296
    :param rbytes:
        Limit on total amount of data to read from this channel, in bytes.

        Default: 4294967296
    :param writes:
        Limit for number of writes to this channel.

        Default: 4294967296
    :param wbytes:
        Limit on total amount of data to be written to this channel, in bytes.

        Default: 4294967296
    :param mode:
        'file', 'char', or 'pipe'. Default is 'file'.
    :param bool is_image:
        `True` if the channel is for a tar image to be mounted into the file
        system, else `False`. Defaults to `False`.
    """

    def __init__(self, uri, alias, access_type,
                 access=_DEFAULT_MOUNT_ACCESS,
                 mount_dir=_DEFAULT_MOUNT_DIR,
                 etag=0,
                 reads=READS_DEFAULT,
                 rbytes=RBYTES_DEFAULT,
                 writes=WRITES_DEFAULT,
                 wbytes=WBYTES_DEFAULT,
                 mode='file',
                 is_image=False):
        self.uri = uri
        self.alias = alias
        self.access_type = access_type
        self.access = access
        self.mount_dir = mount_dir
        self.etag = etag
        self.reads = reads
        self.rbytes = rbytes
        self.writes = writes
        self.wbytes = wbytes
        self.mode = mode
        self.is_image = is_image

    def __eq__(self, other):
        return (
            self.uri == other.uri
            and self.alias == other.alias
            and self.access_type == other.access_type
            and self.mount_dir == other.mount_dir
            and self.etag == other.etag
            and self.reads == other.reads
            and self.rbytes == other.rbytes
            and self.writes == other.writes
            and self.wbytes == other.wbytes
            and self.mode == other.mode
            and self.is_image == other.is_image
        )

    def __str__(self):
        return 'Channel = %s,%s,%s,%s,%s,%s,%s,%s' % (
            self.uri, self.alias, self.access_type, self.etag,
            self.reads, self.rbytes, self.writes, self.wbytes
        )

    def __repr__(self):
        return '<%s>' % self.__str__()

    def to_manifest(self):
        """
        Dump this Channel to the representation required for the manifest file.
        """
        return str(self)

    def to_nvram_fstab(self):
        """
        Dump this Channel to the representation required for the [fstab]
        section of the nvram file.
        """
        return ('channel=%(alias)s,mountpoint=%(mp)s,access=%(access)s,'
                'removable=%(removable)s') % dict(
            alias=self.alias,
            mp=self.mount_dir,
            access=self.access,
            removable='no'
        )

    def to_nvram_mapping(self):
        """
        Dump this Channel to the represention required for the [mapping]
        section of the nvram file.
        """
        return 'channel=%(alias)s,mode=%(mode)s' % dict(
            alias=self.alias, mode=self.mode
        )


class Manifest(object):
    """
    Object representation of a ZeroVM manifest. Includes utilities and sane
    defaults for generating manifest files.

    :param program:
        Path to the nexe for ZeroVM to execute
    :param channels:
        `list` of :class:`Channel` objects.
    :param version:
        ZeroVM manifest format version
    :param timeout:
        Application timeout, in seconds
    :param memory:
        Maximum RAM allocable for the process (in bytes)
    :param node:
        For a single node job, defaults to 1. For multi-node jobs, such as a
        map/reduce job, the node count will increase (1-N).
    :param etag:
        0 (disabled) or 1 (enabled)
    """
    DEFAULT_NODE = 1

    def __init__(self,
                 program,
                 channels,
                 version=MANIFEST_DEFAULTS['version'],
                 timeout=MANIFEST_DEFAULTS['timeout'],
                 memory=MANIFEST_DEFAULTS['memory'],
                 node=DEFAULT_NODE,
                 etag=0):
        self.program = program
        self.channels = channels
        self.version = version
        self.timeout = timeout
        self.memory = memory

        self.node = node
        self.etag = etag

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
            channels='\n'.join([c.to_manifest() for c in self.channels]),
        )
        return manifest


class NVRAM(object):
    """
    :param program_args:
        A `list` of the command args to be run inside ZeroVM. In the case of a
        Python application, this would be something like:

            ['python', '-c', 'print "hello, world"']

    :param channels:
        `list` of :class:`Channel` objects.

    :param env:
        Optional. `dict` of environment settings from zvsh.cfg.

    :param int debug_verbosity:
        Optional. Debug verbosity level, in the range 0..4.
    """

    def __init__(self, program_args, channels, env=None, debug_verbosity=None):
        self.program_args = program_args
        self.channels = channels
        self.env = env
        self.debug_verbosity = debug_verbosity

    def dumps(self):
        nvram_text = NVRAM_TEMPLATE
        args = ' '.join(map(_nvram_escape, self.program_args))

        nvram_text %= dict(
            args=args,
            fstab='\n'.join([c.to_nvram_fstab() for c in self.channels
                             if c.is_image]),
            mapping='\n'.join([c.to_nvram_mapping() for c in self.channels
                               if not c.is_image]),
        )

        if self.env is not None:
            nvram_text += '[env]\n'
            for k, v in self.env.items():
                nvram_text += 'name=%s,value=%s\n' % (k, _nvram_escape(v))
        if self.debug_verbosity is not None:
            nvram_text += '[debug]\nverbosity=%s\n' % self.debug_verbosity

        return nvram_text


def _get_fd_mode(fd):
    """
    Get the mode of a given file descriptor `fd`.

    Can be 'file', 'block', 'pipe', 'char', or other (`None`).
    """
    try:
        mode = os.fstat(fd.fileno()).st_mode
        if stat.S_ISREG(mode):
            return 'file'
        elif stat.S_ISBLK(mode):
            return 'block'
        elif stat.S_ISFIFO(mode):
            return 'pipe'
        elif stat.S_ISCHR(mode):
            return 'char'
        else:
            return None
    except AttributeError:
        return None


def prepare_channels(zvargs, working_dir, node, limits, fstab_channels=None):
    """
    Prepare a list of :class:`Channel` objects from the command line args.

    Channels can come from either files specified with a @ prefix or with the
    `--zvm-image` flag.

    :param zvargs:
        :class:`ZvArgs` instance
    :param str working_dir:
        Working directory to save runtime files to
    :param int node:
        Node number (1-n). Specifies the node ID for a given job (since there
        can be multiple nodes in something like a map/reduce job).
    :param dict limits:
        Read/write limits. Should contain the following keys:

            * reads
            * rbytes
            * writes
            * wbytes

    :param fstab_channels:
        Optional. List of :class:`Channel` objects representing the `[fstab]`
        mappings from zvsh.cfg.

    :returns:
        2-tuple of lists of :class:`Channel` instances: one for manifest
        channels, and one for nvram channels
    """
    manifest_channels = []
    nvram_channels = []

    # The following rules must be applied to determine the channel
    # configuration for the manifest and nvram.
    #
    # if std{in,out,err} is a tty:
    #     include a channel definition in the manifest and nvram [mapping]
    #     channel mode for nvram [mapping] is char
    # elif std{in,out,err} is a file:
    #     include a channel definition in the manifest and nvram [mapping]
    #     channel mode for nvram [mapping] is file
    # else:
    #     include a channels definition in the manifest only

    stdin_chan = Channel('/dev/stdin', '/dev/stdin', SEQ_READ_SEQ_WRITE,
                         reads=limits['reads'], rbytes=limits['rbytes'],
                         writes=0, wbytes=0)
    stdout_chan = Channel(
        '%(wd)s/stdout.%(node)s' % dict(wd=working_dir, node=node),
        '/dev/stdout', SEQ_READ_SEQ_WRITE, reads=0, rbytes=0,
        writes=limits['writes'], wbytes=limits['wbytes']
    )
    stderr_chan = Channel(
        '%(wd)s/stderr.%(node)s' % dict(wd=working_dir, node=node),
        '/dev/stderr', SEQ_READ_SEQ_WRITE, reads=0, rbytes=0,
        writes=limits['writes'], wbytes=limits['wbytes']
    )
    manifest_channels.extend([stdin_chan, stdout_chan, stderr_chan])

    if sys.stdin.isatty():
        stdin_chan.mode = 'char'
        nvram_channels.append(stdin_chan)
    else:
        fd_mode = _get_fd_mode(sys.stdin)
        if fd_mode is not None:
            stdin_chan.mode = fd_mode
            nvram_channels.append(stdin_chan)

    if sys.stdout.isatty():
        stdout_chan.mode = 'char'
        nvram_channels.append(stdout_chan)
    else:
        fd_mode = _get_fd_mode(sys.stdout)
        if fd_mode is not None:
            stdout_chan.mode = fd_mode
            nvram_channels.append(stdout_chan)

    if sys.stderr.isatty():
        stderr_chan.mode = 'char'
        nvram_channels.append(stderr_chan)
    else:
        fd_mode = _get_fd_mode(sys.stderr)
        if fd_mode is not None:
            stderr_chan.mode = fd_mode
            nvram_channels.append(stderr_chan)

    if fstab_channels is not None:
        manifest_channels.extend(fstab_channels)
        nvram_channels.extend(fstab_channels)
    manifest_channels.extend(zvargs.channels)
    nvram_channels.extend(zvargs.channels)

    return manifest_channels, nvram_channels


def _nvram_escape(value):
    r"""Escape value for inclusion as a value in a nvram file.

    The ini-file parser in ZRT is very simple. One quirk is that it
    handles ',' the same as '\n', which means that a value like

      greeting = Hello, World

    will be cut-off after "Hello".

    Values also need protection in other ways:

    * When "args" are loaded, the value is split on ' ' and each
      argument found is then unescaped. This means that each arg need
      to have ' ' escaped.

    * When a "value" is loaded in [env], it is unescaped. It must
      therefore also be escaped.

    This function escapes '\\', '"', ',', ' ', and '\n'. These are the
    characters that conf_parser::unescape_string_copy_to_dest is
    documented to handle and they are sufficient to handle the above
    use cases.

    >>> _nvram_escape('foo, bar')
    'foo\\x2c\\x20bar'
    >>> _nvram_escape('new\nline')
    'new\\x0aline'
    """
    for c in '\\", \n':
        value = value.replace(c, '\\x%02x' % ord(c))
    return value


def _process_images(zvm_images):
    """
    Process a list of the --zvm-image arguments and split them into the
    `path,mount_point,access` components. This returns a generator of
    3-tuples.

    `mount_point` and `access` are optional and will default to `/` and
    `ro`, respectively.

    Example:

    >>> list(_process_images(['/home/user1/foo.tar',
    ...                       '/home/user1/bar.tar,/var/lib',
    ...                       '/home/user1/baz.tar,/usr/lib,rw']))
    [('/home/user1/foo.tar', '/', 'ro'), \
('/home/user1/bar.tar', '/var/lib', 'ro'), \
('/home/user1/baz.tar', '/usr/lib', 'rw')]

    """
    for image in zvm_images:
        image_split = image.split(',')
        # mount_dir and access_type are optional,
        # so defaults are provided:
        mount_dir = _DEFAULT_MOUNT_DIR
        access = _DEFAULT_MOUNT_ACCESS

        if len(image_split) == 1:
            path = image_split[0]
        elif len(image_split) == 2:
            path, mount_dir = image_split
        elif len(image_split) == 3:
            path, mount_dir, access = image_split

        yield path, mount_dir, access


def _get_runtime_file_paths(working_dir, node):
    """
    Generate the runtime files paths for boot, manifest, nvram, stdout, and
    stderr files, and return them as a `OrderedDict` with the following
    structure:

    >>> _get_runtime_file_paths('/home/user1', 1)
    OrderedDict([('manifest', '/home/user1/manifest.1'), \
('nvram', '/home/user1/nvram.1'), \
('stdout', '/home/user1/stdout.1'), \
('stderr', '/home/user1/stderr.1')])


    Note that that paths are created by simply joining `working_dir`, so
    relatve file paths can be used as well:

    >>> _get_runtime_file_paths('foo/', 1)
    OrderedDict([('manifest', 'foo/manifest.1'), \
('nvram', 'foo/nvram.1'), \
('stdout', 'foo/stdout.1'), \
('stderr', 'foo/stderr.1')])
    """
    files_list = ['manifest', 'nvram', 'stdout', 'stderr']

    files = OrderedDict()
    for each in files_list:
        files[each] = os.path.join(working_dir, '%s.%s' % (each, node))

    return files


def _get_nvram(zvargs, channels, env=None):
    """
    :param zvargs:
        :class:`ZvArgs` instance.
    :param channels:
        `list` of :class:`Channel` objects
    :param dict env:
        `dict`-like of environment variable key-value pairs (typically read
        from the zvsh config file)

    :returns:
        A :class:`NVRAM` instance.
    """
    if env is None:
        env = {}
    # combine the configured environment (zvsh.cfg) with env vars supplied on
    # the command line:
    # NOTE(larsbutler): explicit list casts needed here for python3, since
    # .items() returns view objects which cannot be concatenated.
    combined_env = env.copy()
    combined_env.update(zvargs.env)
    return NVRAM([zvargs.args.command] + zvargs.processed_cmd_args, channels,
                 env=combined_env)


def _get_manifest(program, channels, manifest_config):
    """
    :param program:
        path to the nexe on the host file system
    :param channels:
        `list` of :class:`Channel` objects
    :param dict manifest_config:
        Configuration for the manifest. Should contain the following keys::

            * Version
            * Timeout
            * Memory
            * Node

    :returns:
        A :class:`Manifest` instance.
    """
    return Manifest(program,
                    channels,
                    version=manifest_config['Version'],
                    timeout=manifest_config['Timeout'],
                    memory=manifest_config['Memory'],
                    node=manifest_config['Node'])


def _create_fstab_channels(zvconfig, limits):
    """
    :param zvconfig:
        :class:`ZvConfig` instance.
    :param dict limits:
        Read/write limits. Should contain the following keys:

            * reads
            * rbytes
            * writes
            * wbytes

    :returns:
        List of :class:`Channel` objects for `[fstab]` configurations (if any)
        in zvsh.cfg.

        If there are no configurations, an empty list is returned.
    """
    fstab_chans = []
    channel_ordinal = 1

    for host_path, mp_access in zvconfig['fstab'].items():
        mount_point, access = mp_access.split()

        basename = os.path.basename(host_path)
        alias = '/dev/%(ordinal)s.%(fn)s' % dict(ordinal=channel_ordinal,
                                                 fn=basename)

        chan = Channel(host_path, alias, RND_READ_RND_WRITE, access=access,
                       mount_dir=mount_point, is_image=True, **limits)
        fstab_chans.append(chan)
        channel_ordinal += 1

    return fstab_chans


def _maybe_create_working_dir(zvargs):
    if zvargs.args.zvm_save_dir is None:
        # use a temp dir
        working_dir = mkdtemp()
    else:
        # use the specified dir
        working_dir = os.path.abspath(zvargs.args.zvm_save_dir)

    if not os.path.exists(working_dir):
        os.makedirs(working_dir)

    return working_dir


def run_zerovm(zvconfig, zvargs, gdb=False):
    """
    :param zvconfig:
        :class:`ZvConfig` instance.
    :param zvargs:
        :class:`ZvArgs` instance.
    """
    working_dir = _maybe_create_working_dir(zvargs)
    # Read/write limits for channels
    limits = zvconfig['limits']

    fstab_channels = _create_fstab_channels(zvconfig, limits)

    zvargs.process_args(init_channel_ordinal=len(fstab_channels) + 1,
                        limits=limits)

    node = zvconfig['manifest']['Node']

    manifest_channels, nvram_channels = prepare_channels(
        zvargs, working_dir, node, limits, fstab_channels=fstab_channels
    )
    nvram = _get_nvram(zvargs, nvram_channels, env=zvconfig['env'])

    # These files will be generated in the `working_dir`.
    runtime_files = _get_runtime_file_paths(working_dir, node)

    # If any of these files already exist in the target dir,
    # we need to raise an error and halt.
    for file_path in (runtime_files['nvram'], runtime_files['manifest']):
        if os.path.exists(file_path):
            raise RuntimeError("Unable to write '%s': file already exists"
                               % file_path)

    abs_command = os.path.abspath(zvargs.args.command)
    # Is the nexe on the host file system?
    if os.path.exists(abs_command):
        runtime_files['boot'] = abs_command
    else:
        # The nexe must be inside a tar archive
        nexe_target = os.path.join(working_dir, 'boot.%s' % node)
        _extract_nexe(nexe_target,
                      [c.uri for c in manifest_channels if c.is_image],
                      zvargs.args.command)
        runtime_files['boot'] = nexe_target

    # Add another channel for the the nvram to the manifest file:
    nvram_channel = Channel(runtime_files['nvram'], '/dev/nvram',
                            RND_READ_RND_WRITE, **limits)
    manifest = _get_manifest(runtime_files['boot'],
                             manifest_channels + [nvram_channel],
                             zvconfig['manifest'])

    # Now create the runtime files:
    # stdout and stderr are pipes, and can be reused
    # we only need to create them if they don't already exist
    for stdfile in (runtime_files['stdout'], runtime_files['stderr']):
        if not os.path.exists(stdfile):
            os.mkfifo(stdfile)

    with open(runtime_files['manifest'], 'w') as man_fp:
        man_fp.write(manifest.dumps())

    with open(runtime_files['nvram'], 'w') as nvram_fp:
        nvram_fp.write(nvram.dumps())

    # Now that all required files are generated and in place, run:
    try:
        _run_zerovm(working_dir, runtime_files['manifest'],
                    runtime_files['stdout'], runtime_files['stderr'],
                    zvargs.args.zvm_trace, zvargs.args.zvm_getrc)
    finally:
        # If we're using a tempdir for the working files,
        # destroy the directory to clean up.
        if zvargs.args.zvm_save_dir is None:
            shutil.rmtree(working_dir)


def _run_zerovm(working_dir, manifest_path, stdout_path, stderr_path,
                zvm_trace, zvm_getrc):
    """
    :param working_dir:
        Working directory which contains files needed to run ZeroVM (manifest,
        nvram, etc.).
    :param manifest_path:
        Path to the ZeroVM manifest, which should be in `working_dir`.
    :param stdout_path:
        Path to the file into which stdout is written. This file should be in
        `working_dir`.
    :param stderr_path:
        Path to the file into which stderr is written. This file should be in
        `working_dir`.
    :param bool zvm_trace:
        If `True`, enable ZeroVM trace output into `./zvsh.trace.log`.
    :param bool zvm_getrc:
        If `True`, return the ZeroVM exit code instead of the application exit
        code.
    """
    zvm_run = ['zerovm', '-PQ']
    if zvm_trace:
        # TODO(larsbutler): This should not be hard-coded. Can we parameterize
        # this via the command line?
        trace_log = os.path.abspath('zvsh.trace.log')
        zvm_run.extend(['-T', trace_log])
    zvm_run.append(manifest_path)
    runner = ZvRunner(zvm_run, stdout_path, stderr_path, working_dir,
                      getrc=zvm_getrc)
    runner.run()


def _extract_nexe(program_path, tar_images, command):
    """
    Given a `command`, search through the listed tar images
    (`tar_images`) and extract the nexe matching `command` to the target
    `program_path` on the host file system.

    :param program_path:
        Location (including filename) which specifies the destination of the
        extracted nexe.
    :param tar_images:
        `list` of tar image paths.
    :param command:
        The name of a nexe, such as `python` or `myapp.nexe`.
    """
    for zvm_image in tar_images:
        try:
            tf = tarfile.open(zvm_image)
            nexe_fp = tf.extractfile(command)
            with open(program_path, 'w') as program_fp:
                # once we've found the nexe the user wants to run,
                # we're done
                program_fp.write(nexe_fp.read())
                break
        except KeyError:
            # program not found in this image,
            # go to the next and keep searching
            pass
        finally:
            tf.close()
    else:
        raise RuntimeError("ZeroVM executable '%s' not found!" % command)


class ZvArgs:
    """
    :attr parser:
        :class:`argparse.ArgumentParser` instance, used to define the command
        line arguments.
    :attr args:
        :class:`argparse.Namespace` representing the command line arguments.
    """

    def __init__(self):
        self.parser = argparse.ArgumentParser(
            formatter_class=argparse.RawTextHelpFormatter
        )
        self.args = None
        self.channels = []
        self.processed_cmd_args = []
        self.env = OrderedDict()

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
            help='ZeroVM debug verbosity level (0-3)\n',
            type=int,
            default=0,
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

    def process_args(self, limits, init_channel_ordinal=1):
        """
        Process the command line args and populate the following attributes:

            * `channels`: list of :class:`Channel` objects constructed from the
              `--zvm-image` arguments and positional arguments
            * `processed_cmd_args`: processed command args which will be run
              inside the ZeroVM instance, which will include in-memory
              filesystem paths to files specified on the command-line using the
              `@` prefix
            * `env`: dict of environment variable key-value pairs

    :param dict limits:
        Read/write limits. Should contain the following keys:

            * reads
            * rbytes
            * writes
            * wbytes

        :param int init_channel_ordinal:
            Starting ordinal for channels, which will service as a file prefix
            inside the ZeroVM filsystem (for example, "/dev/1.python.tar",
            where "1" is the ordinal prefix; this helps to ensure that multiple
            channels which have the same base filename on the host FS do not
            get mapped to the same ZeroVM mount point).
        """
        channel_ordinal = init_channel_ordinal
        device_fmt = '/dev/%(ordinal)s.%(fn)s'

        for cmd_arg in self.args.cmd_args:
            if cmd_arg.startswith('@'):
                # Trim the leading `@`
                cmd_arg = cmd_arg[1:]
                m = ENV_MATCH.match(cmd_arg)
                if m:
                    self.env[m.group(1)] = m.group(2)
                else:
                    # 1) gather procssed cmd arg
                    abs_filename = os.path.abspath(cmd_arg)
                    filename = os.path.basename(abs_filename)
                    # the name/path for the device in the zerovm filesystem:
                    zvm_device = device_fmt % dict(ordinal=channel_ordinal,
                                                   fn=filename)
                    self.processed_cmd_args.append(zvm_device)
                    # 2) create channel
                    chan = Channel(abs_filename,
                                   zvm_device,
                                   RND_READ_RND_WRITE,
                                   **limits)
                    self.channels.append(chan)
                    channel_ordinal += 1
            else:
                self.processed_cmd_args.append(cmd_arg)

        zvm_images = self.args.zvm_image
        if zvm_images is not None:
            for image, mount_dir, access in _process_images(zvm_images):
                abs_filename = os.path.abspath(image)
                filename = os.path.basename(abs_filename)
                chan = Channel(abs_filename,
                               '/dev/%(ordinal)s.%(fn)s'
                               % dict(ordinal=channel_ordinal, fn=filename),
                               RND_READ_RND_WRITE,
                               access=access,
                               mount_dir=mount_dir,
                               is_image=True,
                               **limits)
                self.channels.append(chan)
                channel_ordinal += 1


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
        self.nvram_reg_files = []
        self.program = None
        self.savedir = None
        self.tmpdir = None
        self.config = config
        self.savedir = savedir
        if self.savedir:
            # user specified a savedir
            self.tmpdir = self.savedir
            if not os.path.exists(self.tmpdir):
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
            self.channel_seq_write_template % (os.path.abspath(self.stdout),
                                               '/dev/stdout'),
            self.channel_seq_write_template % (os.path.abspath(self.stderr),
                                               '/dev/stderr')
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
                    self.nvram_reg_files.append(dev_name)
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
        mapping = ''
        if sys.stdin.isatty() or sys.stdout.isatty() or sys.stderr.isatty():
            if sys.stdin.isatty():
                mapping += 'channel=/dev/stdin,mode=char\n'
            if sys.stdout.isatty():
                mapping += 'channel=/dev/stdout,mode=char\n'
            if sys.stderr.isatty():
                mapping += 'channel=/dev/stderr,mode=char\n'
        for dev in self.nvram_reg_files:
            mapping += 'channel=%s,mode=file\n' % dev
        if mapping:
            nvram += '[mapping]\n' + mapping
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
    # TODO(larsbutler): This type check won't work with python3
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

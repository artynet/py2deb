# Standard library modules.
import fnmatch
import glob
import os
import pipes
import shutil
import sys
import tempfile

# External dependencies
import pip_accel
import pip.exceptions
from debian.deb822 import Deb822
from debian.debfile import DebFile
from deb_pkg_tools import merge_control_fields

# Internal modules
from py2deb.logger import logger
from py2deb.package import Package
from py2deb.util import run, transform_package_name

# Old style of ignoring dependencies on Python packages.
IGNORE_INSTALL_REQUIRES = True

# New style of ignoring dependencies on Python packages.
NO_GUESSING_DEPS = True

class RedirectOutput:

    """
    Make sure all output generated by pip and its subprocesses (python setup.py
    ...) is redirected to the standard error stream. This way we can be sure
    that the standard output stream can be reliably used for our purposes
    (specifically: reporting Debian package dependencies).
    """

    def __init__(self, target):
        self.target = target
        self.stdout = sys.stdout

    def __enter__(self):
        sys.stdout = self.target

    def __exit__(self, exc_type, exc_value, traceback):
        sys.stdout = self.stdout

def convert(pip_args, config, auto_install=False, verbose=False):
    """
    Convert Python packages downloaded using pip-accel to Debian packages.
    """
    # Initialize the build directory.
    build_dir = tempfile.mkdtemp(prefix='py2deb_')
    logger.debug('Created build directory: %s', build_dir)
    # Find package replacements.
    replacements = dict(config.items('replacements'))
    # Tell pip to extract into the build directory
    pip_args.extend(['-b', build_dir])
    # Generate list of requirements.
    requirements = get_required_packages(pip_args,
                                         virtual_prefix=config.get('general', 'virtual-prefix'),
                                         custom_prefix=config.get('general', 'custom-prefix'),
                                         replacements=replacements)
    logger.debug("Required packages: %r", requirements)
    converted = []
    repo_dir = os.path.abspath(config.get('general', 'repository'))
    for package in requirements:
        result = find_build(package, repo_dir)
        if result:
            logger.info('%s has been found in %s, skipping build.',
                         package.debian_name, repo_dir)
            debfile = DebFile(result[-1])
        else:
            logger.info('Starting conversion of %s', package.name)
            debianize(package, verbose)
            #patch_rules(package)
            patch_control(package, replacements, config)
            apply_script(package, config, verbose)
            pip_accel.deps.sanity_check_dependencies(package.name, auto_install)
            debfile = build(package, repo_dir, verbose)
            logger.info('%s has been converted to %s', package.name, package.debian_name)
        converted.append('%(Package)s (=%(Version)s)' % debfile.debcontrol())
    # Clean up the build directory.
    shutil.rmtree(build_dir)
    logger.debug('Removed build directory: %s', build_dir)
    return converted

def find_build(package, repository):
    """
    Find an existing *.deb package that was previously generated.
    """
    return glob.glob(os.path.join(repository, package.debian_file_pattern))

def get_required_packages(pip_args, virtual_prefix, custom_prefix, replacements):
    """
    Find the packages that have to be converted to Debian packages (excludes
    packages that have replacements).
    """
    pip_arguments = ['install', '--ignore-installed'] + pip_args
    # Create a dictionary of packages downloaded by pip-accel.
    packages = {}
    for name, version, directory in get_source_dists(pip_arguments):
        package = Package(name, version, directory, virtual_prefix, custom_prefix)
        packages[package.name] = package
    # Create a list of packages to ignore.
    to_ignore = []
    for pkg_name, package in packages.iteritems():
        if pkg_name in replacements:
            to_ignore.extend(get_related_packages(pkg_name, packages))
    # Yield packages that should be build.
    to_build = []
    for pkg_name, package in packages.iteritems():
        if pkg_name not in to_ignore:
            to_build.append(package)
        else:
            logger.warn('%s is in the ignore list and will not be build.', pkg_name)
    return to_build

def get_related_packages(pkg_name, packages):
    """
    Creates a list of all related packages.
    """
    related = []
    if pkg_name in packages:
        related.append(pkg_name)
        for dependency in packages.get(pkg_name).py_dependencies:
            related.extend(get_related_packages(dependency, packages))
    return related

def get_source_dists(pip_arguments, max_retries=10):
    """
    Download and unpack the source distributions for all dependencies
    specified in the pip command line arguments.
    """
    with RedirectOutput(sys.stderr):
        pip_accel.initialize_directories()
        logger.debug('Passing the following arguments to pip-accel: %s', ' '.join(pip_arguments))
        for i in xrange(max_retries):
            logger.debug('Attempt %i/%i of getting source distributions using pip-accel.',
                         i+1, max_retries)
            try:
                return pip_accel.unpack_source_dists(pip_arguments)
            except pip.exceptions.DistributionNotFound:
                pip_accel.download_source_dists(pip_arguments)
        else:
            raise Exception, 'pip-accel failed to get the source dists %i times.' % max_retries

def debianize(package, verbose):
    """
    Debianize a Python package using stdeb.
    """
    logger.debug('Debianizing %s', package.name)
    python = os.path.join(sys.prefix, 'bin', 'python')
    command = [python, 'setup.py', '--command-packages=stdeb.command', 'debianize']
    if IGNORE_INSTALL_REQUIRES:
        command.append('--ignore-install-requires')
    if run(' '.join(command), package.directory, verbose):
        raise Exception, "Failed to debianize package! (%s)" % package.name
    logger.debug('Debianized %s', package.name)

def patch_rules(package):
    """
    Patch the rules file to prevent dh_python2 from guessing dependencies. This
    only has effect if the 0.6.0+git release of stdeb is used.
    """
    logger.debug('Patching rules file of %s', package.name)
    patch = '\noverride_dh_python2:\n\tdh_python2 --no-guessing-deps\n'
    rules_file = os.path.join(package.directory, 'debian', 'rules')
    lines = []
    with open(rules_file, 'r') as rules:
        lines = rules.readlines()
        for i in range(len(lines)):
            if '%:' in lines[i]:
                lines.insert(i-1, patch)
                break
        else:
            raise Exception, 'Failed to patch %s' % rules_file
    with open(rules_file, 'w+') as rules:
        rules.writelines(lines)
    logger.debug('The rules file of %s has been patched', package.name)

def patch_control(package, replacements, config):
    """
    Patch control file to add dependencies.
    """
    logger.debug('Patching control file of %s', package.name)
    control_file = os.path.join(package.directory, 'debian', 'control')
    with open(control_file, 'r') as handle:
        paragraphs = list(Deb822.iter_paragraphs(handle))
        assert len(paragraphs) == 2, 'Unexpected control file format for %s.' % package.name
    with open(control_file, 'w') as handle:
        virtual_prefix = config.get('general', 'virtual-prefix')
        custom_prefix = config.get('general', 'custom-prefix')
        # Set the package name.
        paragraphs[1]['Package'] = transform_package_name(custom_prefix, package.name)
        # Patch the dependencies.
        paragraphs[1] = merge_control_fields(paragraphs[1], dict(Depends=', '.join(package.debian_dependencies(replacements))))
        # Patch any configured fields.
        paragraphs[1] = merge_control_fields(paragraphs[1], control_patch_cfg(package, config))
        logger.debug("Patched control fields: %r", paragraphs[1])
        paragraphs[0].dump(handle)
        handle.write('\n')
        paragraphs[1].dump(handle)
    logger.debug('The control file of %s has been patched', package.name)

def control_patch_cfg(package, config):
    """
    Creates a Deb822 dictionary used to patch the
    second paragraph of a control file by using
    fields defined in a config file.
    """
    config_fields = Deb822()
    if config.has_section(package.name):
        for name, value in config.items(package.name):
            if name != 'script':
                config_fields[name] = value
    return config_fields

def apply_script(package, config, verbose):
    """
    Checks if a line of shell script is defined in the config and
    executes it with the directory of the package as the current
    working directory.
    """
    if config.has_option(package.name, 'script'):
        command = config.get(package.name, 'script')
        logger.debug('Applying the following script on %s in %s: %s',
                     package.name, package.directory, command)

        if run(command, package.directory, verbose):
            raise Exception, 'Failed to apply script on %s' % package.name

        logger.debug('The script has been applied.')

def build(package, repository, verbose):
    """
    Builds the Debian package using dpkg-buildpackage.
    """
    logger.info('Building %s', package.debian_name)
    # XXX Always run the `dpkg-buildpackage' command in a clean environment.
    # Without this and `py2deb' is running in a virtual environment, the
    # pathnames inside the generated Debian package will refer to the virtual
    # environment instead of the system wide Python installation!
    command = '. /etc/environment && dpkg-buildpackage -us -uc'
    if NO_GUESSING_DEPS:
        # XXX stdeb 0.6.0+git uses dh_python2, which guesses dependencies
        # by default. We don't want this so we override this behavior.
        os.environ['DH_OPTIONS'] = '--no-guessing-deps'
    if run(command, package.directory, verbose):
        raise Exception, 'Failed to build %s' % package.debian_name
    logger.debug("Scanning for generated Debian packages ..")
    parent_directory = os.path.dirname(package.directory)
    for filename in os.listdir(parent_directory):
        if filename.endswith('.deb'):
            pathname = os.path.join(parent_directory, filename)
            logger.debug("Considering file: %s", pathname)
            if fnmatch.fnmatch(filename, '%s_*.deb' % package.debian_name):
                logger.info('Build of %s succeeded, checking package with Lintian ..', pathname)
                os.system('lintian %s' % pipes.quote(pathname))
                logger.info('Moving %s to %s', pathname, repository)
                shutil.move(pathname, repository)
                return DebFile(os.path.join(repository, filename))
    else:
        raise Exception, 'Could not find build of %s' % package.debian_name

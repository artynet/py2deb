# Standard library modules.
import fnmatch
import glob
import hashlib
import os
import os.path
import shutil
import sys
import tempfile
from subprocess import Popen, PIPE, STDOUT
from ConfigParser import ConfigParser

# External dependencies.
import pkg_resources
import pip_accel
import pip.exceptions
from debian.deb822 import Deb822
from debian.debfile import DebFile

# Internal modules.
from py2deb.config import config_dir, PKG_REPO, DEPENDENCY_STORE
from py2deb.util.package import Requirement, Package
from py2deb.util import compact

class Converter:
    '''
    Converts a list of python packages to debian packages.
    '''

    def __init__(self, requirements_file):
        self.packages = []
        self.builddir = tempfile.mkdtemp(prefix='py2deb_')
        self.requirements_file = requirements_file
        self.config = ConfigParser()
        self.config.read(os.path.join(config_dir, 'control.ini'))

    def convert(self):
        '''
        Start the conversion.
        '''
        self.preinstall()
        self.packages.extend(self.get_required_packages())

        for package in self.packages:
            if package.is_built:
                print '%s is already built, skipping...' % (package.plname,)
                continue

            print 'Converting %s-%s ...' % (package.name, package.version)

            self.apply_workarounds(package)
            self.debianize(package)
            self.parse_egg_req(package)
            self.patch_rules(package)
            self.patch_control(package)
            self.install_build_dep(package)
            self.build(package)

            print 'Converted %s-%s to %s' % (package.name, package.version, package.debfile)

        print '\nConversion completed!'

        self.persist_dependencies()

    def preinstall(self):
        ''''''
        if self.config.has_section('preinstall'):
            dependencies = []
            for name, value in self.config.items('preinstall'):
                dependencies.extend(value.split())
            self._install_build_dep(*dependencies)

    def get_required_packages(self):
        '''
        Install global build dependencies and any dependencies needed to
        evaluate setup.py scripts like the one from MySQL-python which
        requires libmysqlclient before setup.py works.
        '''
        sdists = self.get_source_dists(['install', '--ignore-installed', '-b', 
                                  self.builddir, '-r', self.requirements_file])

        # Remove packages if they're in the ignore list.
        sdists = [p for p in sdists if not self.config.has_option('ignore', p[0].lower())]

        return [Package(p[0], p[1], p[2]) for p in sdists]        

    def get_source_dists(self, pip_arguments, max_retries=10):
        """
        Download and unpack the source distributions for all dependencies specified
        in the pip command line arguments.
        """
        pip_accel.initialize_directories()
        i = 1
        while i < max_retries:
            try:
                return pip_accel.unpack_source_dists(pip_arguments)
            except pip.exceptions.DistributionNotFound:
                pip_accel.download_source_dists(pip_arguments)
            i += 1
        raise Exception, "pip failed %i times!" % max_retries

    def persist_dependencies(self):
        '''
        Persist the converted requirements in the format of Debian package names
        which can be directly added to the dependencies of the Debian package
        that contains the code base which needs the requirements.
        '''
        deplist = []
        for pkg in self.packages:
            debfile = DebFile(os.path.join(PKG_REPO, pkg.debfile))
            deplist.append('%(Package)s (=%(Version)s)' % debfile.debcontrol())

        with open(self.find_dependency_file(), 'w') as handle:
            handle.write(', '.join(deplist))

    def recall_dependencies(self):
        '''
        Recall the previously persisted Debianized dependencies.
        '''
        dependency_file = self.find_dependency_file()
        if not os.path.isfile(dependency_file):
            raise Exception, compact("Could not recall dependencies: \
                                      This requirements file has not yet been \
                                      converted with py2deb on this machine.")

        with open(dependency_file) as handle:
            return handle.read()

    def find_dependency_file(self):
        '''
        Find the absolute path of the text file where the Debianized dependency
        names and versions corresponding to the requirements.txt file are
        stored.
        '''
        with open(self.requirements_file) as handle:
            context = hashlib.sha1()
            context.update(handle.read())
            fingerprint = context.hexdigest()
        if not os.path.isdir(DEPENDENCY_STORE):
            os.makedirs(DEPENDENCY_STORE)
        return os.path.join(DEPENDENCY_STORE, '%s.txt' % fingerprint)

    def apply_workarounds(self, package):
        '''
        Fabric includes a bundled Paramiko, which makes pl-python-fabric
        conflict with pl-python-paramiko... We patch the source distribution to
        remove the bundled version of Paramiko from the Fabric source package.
        '''
        if package.name.lower() == 'fabric':
            directory = os.path.join(package.directory, 'paramiko')
            if os.path.isdir(directory):
                shutil.rmtree(directory)

    def debianize(self, package):
        '''
        Debianize a python package using stdeb.
        '''
        os.chdir(package.directory)
        python = os.path.join(sys.prefix, 'bin', 'python')
        p = Popen([python, 'setup.py', '--command-packages=stdeb.command', 'debianize',
                  '--ignore-install-requires'], #For pypi version of stdeb
                  stdout=PIPE, stderr=STDOUT)
        stddata = p.communicate()

        if p.returncode > 0:
            print stddata[0]
            raise Exception('Failed to debianize %s' % (package.name,))

    def parse_egg_req(self, package):
        '''
        Parse .egg-info/requires.txt for dependencies.
        '''
        pattern = os.path.join(package.directory, 'pip-egg-info/*.egg-info/requires.txt')
        matches = glob.glob(pattern)
        if len(matches) == 1:
            with open(matches[0]) as r:
                for line in r.readlines():
                    if not line.strip():
                       continue
                    if line.startswith('['):
                        break
                    req = pkg_resources.Requirement.parse(line)
                    if self.config.has_option('replacements', req.key):
                        name = self.config.get('replacements', req.key)
                        req = pkg_resources.Requirement(name, req.specs, req.extras)
                        package.add_requirement(Requirement(req, translatable=False))
                    else:
                        package.add_requirement(Requirement(req, translatable=True))

    def patch_rules(self, package):
        '''
        Patch rules file to prevent dh_python2 to guess dependencies.
        This is only needed if the latest stdeb release from github is used.
        '''
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
                raise Exception('Failed to patch %s' % (rules_file,))

        with open(rules_file, 'w+') as rules:
            rules.writelines(lines)

    def patch_control(self, package):
        '''
        Patch control file to add dependencies.
        '''
        control_file = os.path.join(package.directory, 'debian', 'control')

        with open(control_file, 'r') as control:
            paragraphs = list(Deb822.iter_paragraphs(control))
            assert len(paragraphs) == 2, 'Unexpected control file format for %s.' % (package.name,)

        with open(control_file, 'w+') as control:
            control_dict_conf = self._control_patch(package.name)
            control_dict_pkg = package.control_patch()

            for field in control_dict_conf:
                paragraphs[1].merge_fields(field, control_dict_conf)

            for field in control_dict_pkg:
                paragraphs[1].merge_fields(field, control_dict_pkg)

            paragraphs[1]['Package'] = package.plname

            paragraphs[0].dump(control)
            control.write('\n')
            paragraphs[1].dump(control)

    def _control_patch(self, pkg_name):
        fields = []
        if self.config.has_section(pkg_name):
            fields = self.config.items(pkg_name)

        return Deb822(dict((k.title(), v) for k, v in fields
                           if  k.lower() != 'build-depends'))

    def install_build_dep(self, package):
        '''
        Install build dependencies if needed.
        '''
        if self.config.has_option(package.name, 'build-depends'):
            bdep = self.config.get(package.name, 'build-depends')
            self._install_build_dep(*bdep.split())

    def _install_build_dep(self, *packages):
        p = Popen(['sudo', 'apt-get', 'install', '-y'] + list(packages))
        p.wait()
        if p.returncode > 0:
            raise Exception('Failed to install build dependencies: %s' % (packages,))

    def build(self, package):
        '''
        Builds the debian package using dpkg-buildpackage.
        '''
        os.chdir(package.directory)

        p = Popen(['dpkg-buildpackage', '-us', '-uc'])
        p.wait()

        if p.returncode > 0:
            raise Exception('Failed to build %s' % (package.plname,))

        topdir = os.path.dirname(package.directory)
        for item in os.listdir(topdir):
            if fnmatch.fnmatch(item, '%s_*.deb' % package.plname):
                source = os.path.join(topdir, item)
                shutil.move(source, PKG_REPO)
                print 'Moved %s to %s' % (item, PKG_REPO)
                break
        else:
            raise Exception("Could not find build of %s" % (package.plname,))

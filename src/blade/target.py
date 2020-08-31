# Copyright (c) 2011 Tencent Inc.
# All rights reserved.
#
# Author: Michaelpeng <michaelpeng@tencent.com>
# Date:   October 20, 2011


"""
 This is the target module which is the super class of all of the targets.
"""

from __future__ import absolute_import

import os
import re

from blade import config
from blade import console
from blade.blade_util import var_to_list, iteritems, source_location, md5sum


# Location reference macro regex
LOCATION_RE = re.compile(r'\$\(location\s+(\S*:\S+)(\s+\w*)?\)')


def _normalize_one(target, working_dir):
    """Normalize target from command line form into canonical form.

    Target canonical form: dir:name
        dir: relative to blade_root_dir, use '.' for blade_root_dir
        name: name  if target is dir:name
              '*'   if target is dir
              '...' if target is dir/...
    """
    if target.startswith('//'):
        target = target[2:]
    elif target.startswith('/'):
        console.error('Invalid target "%s" starting from root path.' % target)
    else:
        if working_dir != '.':
            target = os.path.join(working_dir, target)

    if ':' in target:
        path, name = target.rsplit(':', 1)
    else:
        if target.endswith('...'):
            path = target[:-3]
            name = '...'
        else:
            path = target
            name = '*'
    path = os.path.normpath(path)
    return '%s:%s' % (path, name)


def normalize(targets, working_dir):
    """Normalize target list from command line form into canonical form."""
    return [_normalize_one(target, working_dir) for target in targets]


def match(target_id, pattern):
    """Check whether a atrget id match a target pattern"""
    t_path, t_name = target_id.split(':')
    p_path, p_name = pattern.split(':')

    if p_name == '...':
        return t_path == p_path or t_path.startswith(p_path) and t_path[len(p_path)] == os.sep
    if p_name == '*':
        return t_path == p_path
    return target_id == pattern


class Target(object):
    """Abstract target class.

    This class should be derived by subclass like CcLibrary CcBinary
    targets, etc.

    """

    def __init__(self,
                 name,
                 type,
                 srcs,
                 deps,
                 visibility,
                 kwargs):
        """Init method.

        Init the target.

        """
        from blade import build_manager  # pylint: disable=import-outside-toplevel
        self.blade = build_manager.instance
        self.build_dir = self.blade.get_build_dir()
        current_source_path = self.blade.get_current_source_path()
        self.target_database = self.blade.get_target_database()

        self.name = name
        self.path = current_source_path
        # The unique key of this target, for internal use mainly.
        self.key = '%s:%s' % (current_source_path, name)
        # The full qualified target id, to be displayed in diagnostic message
        self.fullname = '//' + self.key
        self.source_location = source_location(os.path.join(current_source_path, 'BUILD'))
        self.type = type
        self.srcs = srcs
        self.deps = []
        self.expanded_deps = []
        self.visibility = 'PUBLIC'

        if not name:
            self.fatal('Missing "name"')

        # Keep track of target filess generated by this target. Note that one target rule
        # may correspond to several target files, such as:
        # proto_library: static lib/shared lib/jar variables
        self.__targets = {}
        self.__default_target = ''
        self.__clean_list = []  # Paths to be cleaned

        # Target releated attributes, they should be set only before generating build rules.
        self.attr = {}

        # For temporary, mutable fields only, their values should not relate to rule_hash
        self.data = {}

        # TODO: Remove it
        self.attr['test_timeout'] = config.get_item('global_config', 'test_timeout')

        self._check_name()
        self._check_kwargs(kwargs)
        self._check_srcs()
        self._check_deps(deps)
        self._init_target_deps(deps)
        self._init_visibility(visibility)
        self.__build_rules = None
        self.__rule_hash = None  # Cached rule hash

    def dump(self):
        """Dump to a dict"""
        target = {
            'type': self.type,
            'path': self.path,
            'name': self.name,
            'srcs': self.srcs,
            'deps': self.deps,
            'visibility': self.visibility,
        }
        target.update(self.attr)
        return target

    def _rule_hash_entropy(self):
        """
        Add more entropy to rule hash.

        Can be override in sub classes, must return a dict{string:value}.

        The default implementation is return the `attr` member, but you can return lesser or more
        elements to custom the final result.
        For example, you can remove unrelated members in `attr` which doesn't affect build and must
        add extra elements which may affect build.
        """
        return self.attr

    def rule_hash(self):
        """Calculate a hash string to be used to judge whether regenerate per-target ninja file"""
        if self.__rule_hash is None:
            # All build related factors should be added to avoid outdated ninja file beeing used.
            entropy = {
                'blade_revision': self.blade.revision(),
                'config': config.digest(),
                'type': self.type,
                'name': self.name,
                'srcs': self.srcs,
            }
            deps = []
            for dkey in self.deps:
                dep = self.target_database[dkey]
                deps.append(dep.rule_hash())
            entropy['deps'] = deps

            # Add more entropy
            entropy.update(self._rule_hash_entropy())

            # Sort to make the result stable
            entropy_str = str(sorted(entropy.items()))

            # Entropy dict can't cantains normal object, because it's default repr contains address,
            # which is changed in different build, so it should not be used as stable hash entropy.
            # If this assert failed, remove the culprit element from entropy if it is unrelated or
            # override it's `__repe__` if it is related.
            assert ' object at 0x' not in entropy_str
            self.__rule_hash = md5sum(entropy_str)
        return self.__rule_hash

    def _format_message(self, level, msg):
        return '%s %s: %s: %s' % (self.source_location, level, self.name, msg)

    def debug(self, msg):
        """Print message with target full name prefix"""
        console.debug(self._format_message('debug', msg), prefix=False)

    def info(self, msg):
        """Print message with target full name prefix"""
        console.info(self._format_message('info', msg), prefix=False)

    def warning(self, msg):
        """Print message with target full name prefix"""
        console.warning(self._format_message('warning', msg), prefix=False)

    def error(self, msg):
        """Print message with target full name prefix"""
        console.error(self._format_message('error', msg), prefix=False)

    def fatal(self, msg, code=1):
        """Print message with target full name prefix and exit"""
        # NOTE: VSCode's problem matcher doesn't recognize 'fatal', use 'error' instead
        console.fatal(self._format_message('error', msg), code=code, prefix=False)

    def _prepare_to_generate_rule(self):
        """Should be overridden. """
        self.error('_prepare_to_generate_rule should be overridden in subclasses')

    def _check_name(self):
        if '/' in self.name:
            self.error('Invalid target name, should not contain dir part')

    def _check_kwargs(self, kwargs):
        if kwargs:
            self.error('Unrecognized options %s' % kwargs)

    def _allow_duplicate_source(self):
        """Whether the target allows duplicate source file with other targets"""
        return False

    # Keep the relationship of all src -> target.
    # Used by build rules to ensure that a source file occurs in
    # exactly one target(only library target).
    __src_target_map = {}

    def _check_srcs(self):
        """Check source files.

        """
        dups = []
        srcset = set()
        for s in self.srcs:
            if s in srcset:
                dups.append(s)
            else:
                srcset.add(s)
        if dups:
            self.error('Duplicate source file paths: %s ' % dups)

        # Check if one file belongs to two different targets.
        action = config.get_item('global_config', 'duplicated_source_action')
        for src in self.srcs:
            if '..' in src or src.startswith('/'):
                self.error('Invalid source file path: %s. can only be relative path, and must '
                           'in current directory or subdirectories.' % src)

            full_src = os.path.normpath(os.path.join(self.path, src))
            target = self.fullname, self._allow_duplicate_source()
            if full_src not in Target.__src_target_map:
                Target.__src_target_map[full_src] = target
            else:
                target_existed = Target.__src_target_map[full_src]
                if target_existed != target:
                    # Always preserve the target which disallows
                    # duplicate source files in the map
                    if target_existed[1]:
                        Target.__src_target_map[full_src] = target
                    elif target[1]:
                        pass
                    else:
                        message = '"%s" is already in srcs of "%s"' % (src, target_existed[0])
                        if action == 'error':
                            self.error(message)
                        elif action == 'warning':
                            self.warning(message)

    def _add_hardcode_library(self, hardcode_dep_list):
        """Add hardcode dep list to key's deps. """
        for dep in hardcode_dep_list:
            if not dep.startswith('//') and not dep.startswith('#'):
                dep = '//' + dep
            dkey = self._unify_dep(dep)
            if dkey[0] == '#':
                self._add_system_library(dkey, dep)
            if dkey not in self.expanded_deps:
                self.expanded_deps.append(dkey)

    def _add_system_library(self, key, name):
        """Add system library entry to database. """
        if key not in self.target_database:
            lib = SystemLibrary(name)
            self.blade.register_target(lib)

    def _add_location_reference_target(self, m):
        """

        Parameters
        -----------
        m: A match object capturing the key and type of the referred target

        Returns
        -----------
        (key, type): the key and type of the referred target

        Description
        -----------
        Location reference makes it possible to refer to the build output of
        another target in the code base.

        General form:
            $(location //path/to:target)

        Some target types may produce more than one output according to the
        build options. Then each output can be referenced by an additional
        type tag:
            $(location //path:name)         # default target output
            $(location //path:name jar)     # jar output
            $(location //path:name so)      # so output

        Note that this method accepts a match object instead of a simple str.
        You could match/search/sub location references in a string with functions
        or RegexObject in re module. For example:

            m = {location regular expression}.search(s)
            if m:
                key, type = self._add_location_reference_target(m)
            else:
                # Not a location reference

        """
        assert m

        key, type = m.groups()
        if not type:
            type = ''
        type = type.strip()
        key = self._unify_dep(key)
        if key not in self.expanded_deps:
            self.expanded_deps.append(key)
        if key not in self.deps:
            self.deps.append(key)
        return key, type

    def _unify_dep(self, dep):
        """Unify dep to key"""
        if dep[0] == ':':
            # Depend on library in current directory
            dkey = (os.path.normpath(self.path), dep[1:])
        elif dep.startswith('//'):
            # Depend on library in remote directory
            if not ':' in dep:
                raise Exception('Wrong dep format "%s" in %s' % (dep, self.fullname))
            (path, lib) = dep[2:].rsplit(':', 1)
            dkey = (os.path.normpath(path), lib)
        elif dep.startswith('#'):
            # System libaray, they don't have entry in BUILD so we need
            # to add deps manually.
            dkey = ('#', dep[1:])
            self._add_system_library(':'.join(dkey), dep)
        else:
            # Depend on library in relative subdirectory
            if not ':' in dep:
                raise Exception('Wrong format in %s' % self.fullname)
            (path, lib) = dep.rsplit(':', 1)
            if '..' in path:
                raise Exception("Don't use '..' in path")
            dkey = (os.path.normpath('%s/%s' % (
                self.path, path)), lib)

        return ':'.join(dkey)

    def _init_target_deps(self, deps):
        """Init the target deps.

        Parameters
        -----------
        deps: the deps list in BUILD file.

        Description
        -----------
        Add target into target database and init the deps list.

        """
        for d in deps:
            dkey = self._unify_dep(d)
            if dkey not in self.expanded_deps:
                self.expanded_deps.append(dkey)
            if dkey not in self.deps:
                self.deps.append(dkey)

    def _check_format(self, t):
        """

        Parameters
        -----------
        t: could be a dep or visibility specified in BUILD file

        Description
        -----------
        Do some basic format check.

        """
        if not (t.startswith(':') or t.startswith('#') or
                t.startswith('//') or t.startswith('./')):
            self.error('Invalid format %s.' % t)
        if t.count(':') > 1:
            self.error("Invalid format %s, missing ',' between labels?" % t)

    def _check_deps(self, deps):
        """_check_deps

        Parameters
        -----------
        deps: the deps list in BUILD file

        Description
        -----------
        Check whether deps are in valid format.

        """
        for dep in deps:
            self._check_format(dep)

    def _init_visibility(self, visibility):
        """

        Parameters
        -----------
        visibility: the visibility list in BUILD file

        Description
        -----------
        Visibility determines whether another target is able to depend
        on this target.

        Visibility specify a list of targets in the same form as deps,
        i.e. //path/to:target. The default value of visibility is PUBLIC,
        which means this target is visible globally within the code base.
        Note that targets inside the same BUILD file are always visible
        to each other.

        """
        if visibility is None:
            return

        visibility = var_to_list(visibility)
        if visibility == ['PUBLIC']:
            return

        self.visibility = []
        for v in visibility:
            self._check_format(v)
            key = self._unify_dep(v)
            if key not in self.visibility:
                self.visibility.append(key)

    def _check_deprecated_deps(self):
        """check that whether it depends upon deprecated target.
        It should be overridden in subclass.
        """

    def _expand_deps_generation(self):
        """Expand the generation process and generated rules of dependencies.

        Such as, given a proto_library target, it should generate Java rules
        in addition to C++ rules once it's depended by a java_library target.
        """

    def _get_java_pack_deps(self):
        """
        Return java package dependencies excluding provided dependencies

        target jars represent a path to jar archive. Each jar is built by
        java_library(prebuilt)/scala_library/proto_library.

        maven jars represent maven artifacts within local repository built
        by maven_jar(...).

        Returns:
            A tuple of (target jars, maven jars)
        """
        # TODO(chen3feng): put to `data`
        return [], []

    def _source_file_path(self, name):
        """Expand the the source file name to full path"""
        return os.path.normpath(os.path.join(self.path, name))

    def _target_file_path(self, file_name):
        """Return the full path of file name in the target dir"""
        return os.path.normpath(os.path.join(self.build_dir, self.path, file_name))

    def _remove_build_dir_prefix(self, path):
        """Remove the build dir prefix of path (e.g. build64_release/)
        Args:
            path:str, the full path starts from the workspace root
        """
        prefix = self.build_dir + os.sep
        if path.startswith(prefix):
            return path[len(prefix):]
        return path

    def _add_target_file(self, label, path):
        """
        Parameters
        -----------
        label: label of the target file as key in the dictionary
        path: the path of target file as value in the dictionary

        Description
        -----------
        Keep track of the output files built by the target itself.
        Set the default target if needed.
        """
        self.__targets[label] = path
        if not self.__default_target:
            self.__default_target = path

    def _add_default_target_file(self, label, path):
        """
        Parameters
        -----------
        label: label of the target file as key in the dictionary
        path: the path of target file as value in the dictionary

        Description
        -----------
        Keep track of the default target file which could be referenced
        later without specifying label
        """
        self.__default_target = path
        self._add_target_file(label, path)

    def _get_target_file(self, label=''):
        """
        Parameters
        -----------
        label: label of the file built by the target

        Returns
        -----------
        The target file path or list of file paths

        Description
        -----------
        Return the target file path corresponding to the specified label,
        return empty if label doesn't exist in the dictionary
        """
        self.get_rules()  # Ensure rules were generated
        if label:
            return self.__targets.get(label, '')
        return self.__default_target

    def _get_target_files(self):
        """
        Returns
        -----------
        All the target files built by the target itself
        """
        self.get_rules()  # Ensure rules were generated
        results = set()
        for _, v in iteritems(self.__targets):
            if isinstance(v, list):
                results.update(v)
            else:
                results.add(v)
        return sorted(results)

    def _remove_on_clean(self, *paths):
        """Add paths to clean list, to be removed in clean sub command.
        In most cases, you needn't to call this function manually, because in the `ninja_build`,
        the outputs will be used to call this function defaultly, unless you need to clean extra
        generated files.
        """
        self.__clean_list += paths

    def get_clean_list(self):
        """Collect paths to be cleaned"""
        return self.__clean_list

    def _write_rule(self, rule):
        """_write_rule.
        Append the rule to the buffer at first.
        Args:
            rule: the rule generated by certain target
        """
        self.__build_rules.append('%s\n' % rule)

    def ninja_rules(self):
        """Generate ninja rules for specific target. """
        raise NotImplementedError(self.fullname)

    def ninja_build(self, rule, outputs, inputs=None,
                    implicit_deps=None, order_only_deps=None,
                    variables=None, implicit_outputs=None, clean=None):
        """Generate a ninja build statement with specified parameters.
        Args:
            clean:list[str], files to be removed on clean, defaults to outputs + implicit_outputs,
                you can pass a empty list to prevent cleaning. (For example, if you want to  remove
                the entire outer dir instead of single files)
            See ninja documents for description for other args.
        """
        outputs = var_to_list(outputs)
        implicit_outputs = var_to_list(implicit_outputs)
        outs = outputs[:]
        if implicit_outputs:
            outs.append('|')
            outs += implicit_outputs
        ins = var_to_list(inputs)
        if implicit_deps:
            ins.append('|')
            ins += var_to_list(implicit_deps)
        if order_only_deps:
            ins.append('||')
            ins += var_to_list(order_only_deps)
        self._write_rule('build %s: %s %s' % (' '.join(outs), rule, ' '.join(ins)))
        clean = (outputs + implicit_outputs) if clean is None else var_to_list(clean)
        if clean:
            self._remove_on_clean(*clean)

        if variables:
            assert isinstance(variables, dict)
            for name, v in iteritems(variables):
                assert v is not None
                if v:
                    self._write_rule('  %s = %s' % (name, v))
                else:
                    self._write_rule('  %s =' % name)
        self._write_rule('')  # An empty line to improve readability

    def get_rules(self):
        """Return generated build rules. """
        # Add a cache to make it idempotent
        if self.__build_rules is None:
            self.__build_rules = []
            self.ninja_rules()
        return self.__build_rules


class SystemLibrary(Target):
    def __init__(self, name):
        name = name[1:]
        super(SystemLibrary, self).__init__(
                name=name,
                type='system_library',
                srcs=[],
                deps=[],
                visibility=['PUBLIC'],
                kwargs={})
        self.path = '#'
        self.key = '#:' + name
        self.fullname = '//' + self.key

    def ninja_rules(self):
        pass

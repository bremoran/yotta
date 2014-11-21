# Copyright 2014 ARM Limited
#
# Licensed under the Apache License, Version 2.0
# See LICENSE file for details.

# standard library modules, , ,
import string
import os
import logging
import re
import itertools
from collections import defaultdict

# Cheetah, pip install cheetah, string templating, MIT
import Cheetah.Template

# fsutils, , misc filesystem utils, internal
import fsutils
# validate, , validate various things, internal
import validate

CMakeLists_Template = '''
# NOTE: This file is generated by yotta: changes will be overwritten!

#if $toplevel
cmake_minimum_required(VERSION 2.8.11)

# always use the CMAKE_MODULE_PATH-provided .cmake files, even when including
# from system directories:
cmake_policy(SET CMP0017 OLD)

# toolchain file for $target_name
set(CMAKE_TOOLCHAIN_FILE "${escapeBackslash($toolchain_file)}")

$set_targets_like
#end if

project($component_name)

# include root directories of all components we depend on (directly and
# indirectly, including ourself)
$include_root_dirs

# recurse into dependencies that aren't built elsewhere
$add_depend_subdirs

#if $include_sys_dirs
# Some components (I'm looking at you, libc), need to export system header
# files with no prefix, these directories are listed in the component
# description files:
$include_sys_dirs
#end if

#if $include_other_dirs
# And others (typically CMSIS implementations) need to export non-system header
# files. Please don't use this facility. Please. It's much, much better to fix
# implementations that import these headers to import them using the full path.
$include_other_dirs
#end if

#if $set_objc_flags
# CMake doesn't have native support for Objective-C specific flags, these are
# specified by any depended-on objc runtime using secret package properties...
set(CMAKE_OBJC_FLAGS "$set_objc_flags")
#end if

# Build targets may define additional preprocessor definitions for all
# components to use (such as chip variant information)
add_definitions($yotta_target_definitions)

# Provide the version of the component being built, in case components want to
# embed this into compiled libraries
set(YOTTA_COMPONENT_VERSION "$component_version")

# recurse into subdirectories for this component, using the two-argument
# add_subdirectory because the directories referred to here exist in the source
# tree, not the working directory
#for ($srcdir, $workingdir) in $add_own_subdirs
add_subdirectory(
    "${escapeBackslash($srcdir)}"
    "${escapeBackslash($workingdir)}"
)
#end for

'''

Subdir_CMakeLists_Template = '''
\# NOTE: This file is generated by yotta: changes will be overwritten!

cmake_minimum_required(VERSION 2.8.11)

include_directories("${escapeBackslash($source_directory)}")

#for $lang in $languages
set(YOTTA_AUTO_${lang.upper}_FILES
#for $file_name, $language in $source_files
#if $language in $lang
    "${escapeBackslash($file_name)}"
#end if
#end for
)

#end for
#if $resource_files
set(YOTTA_AUTO_RESOURCE_FILES
    #echo '    ' + '\\n    '.join('"'+$escapeBackslash(x)+'"' for x in $resource_files) + '\\n'
)
#end if

#if $executable
add_executable($object_name
#for $lang in $languages
    \${YOTTA_AUTO_${lang.upper}_FILES}
#end for
#if $resource_files
    \${YOTTA_AUTO_RESOURCE_FILES}
#end if
)
#else
add_library($object_name
#for $lang in $languages
    \${YOTTA_AUTO_${lang.upper}_FILES}
#end for
#if $resource_files
    \${YOTTA_AUTO_RESOURCE_FILES}
#end if
)
#end if

#if $resource_files
set_target_properties($object_name
    PROPERTIES
    RESOURCE "\${YOTTA_AUTO_RESOURCE_FILES}"
)
#end if

#if 'objc' in $languages
\# no proper CMake support for objective-c flags :(
set_target_properties($object_name PROPERTIES
    COMPILE_FLAGS "\${CMAKE_OBJC_FLAGS}"
)
#end if
#for $file_name, $language in $source_files
#if 'plist' in $language
set_target_properties($object_name PROPERTIES
    MACOSX_BUNDLE_INFO_PLIST ${escapeBackslash($file_name)}
)
#end if
#end for

target_link_libraries($object_name
    #echo '    ' + '\\n    '.join($link_dependencies) + '\\n'
)

'''

#this is a Cheetah template
Test_CMakeLists_Template = '''
\# NOTE: This file is generated by yotta: changes will be overwritten!

enable_testing()

include_directories("${escapeBackslash($source_directory)}")

#for $file_names, $object_name, $languages in $tests
add_executable($object_name
    #echo '    ' + '\\n    '.join('"'+$escapeBackslash(x)+'"' for x in $file_names) + '\\n'
)
#if 'objc' in $languages
\# no proper CMake support for objective-c flags :(
set_target_properties($object_name PROPERTIES
    COMPILE_FLAGS "\${CMAKE_OBJC_FLAGS}"
)
#end if
target_link_libraries($object_name
    #echo '    ' + '\\n    '.join($link_dependencies) + '\\n'
)
add_test($object_name $object_name)

#end for
'''

Dummy_CMakeLists_Template = '''
\# NOTE: This file is generated by yotta: changes will be overwritten!

add_library($libname ${escapeBackslash($cfile_name)})

target_link_libraries($libname
    #echo '    ' + '\\n    '.join($link_dependencies) + '\\n'
)

'''

logger = logging.getLogger('cmakegen')

Ignore_Subdirs = set(('build','yotta_modules', 'yotta_targets', 'CMake'))

def escapeBackslash(s):
    return s.replace('\\', '\\\\')

class SourceFile(object):
    def __init__(self, fullpath, relpath, lang):
        super(SourceFile, self).__init__()
        self.fullpath = fullpath
        self.relpath = relpath
        self.lang = lang
    def __repr__(self):
        return self.fullpath
    def lang(self):
        return self.lang

class CMakeGen(object):
    def __init__(self, directory, target):
        super(CMakeGen, self).__init__()
        self.buildroot = directory
        logger.info("generate for target: %s" % target)
        self.target = target
    
    def _writeFile(self, path, contents):
        dirname = os.path.dirname(path)
        fsutils.mkDirP(dirname)
        self.writeIfDifferent(path, contents)

    def generateRecursive(self, component, all_components, builddir=None, processed_components=None):
        ''' generate top-level CMakeLists for this component and its
            dependencies: the CMakeLists are all generated in self.buildroot,
            which MUST be out-of-source

            !!! NOTE: experimenting with a slightly different way of doing
            things here, this function is a generator that yields any errors
            produced, so the correct use is:

            for error in gen.generateRecursive(...):
                print error
        '''
        if builddir is None:
            builddir = self.buildroot
        if processed_components is None:
            processed_components = dict()
        if not self.target:
            yield 'Target "%s" is not a valid build target' % self.target

        toplevel = not len(processed_components)
    
        logger.debug('generate build files: %s (target=%s)' % (component, self.target))
        # because of the way c-family language includes work we need to put the
        # public header directories of all components that this component
        # depends on (directly OR indirectly) into the search path, which means
        # we need to first enumerate all the direct and indirect dependencies
        recursive_deps = component.getDependenciesRecursive(
            available_components = all_components,
                          target = self.target,
                  available_only = True
        )
        dependencies = component.getDependencies(
                  all_components,
                          target = self.target,
                  available_only = True
        )

        for name, dep in dependencies.items():
            if not dep:
                yield 'Required dependency "%s" of "%s" is not installed.' % (name, component)
        # ensure this component is assumed to have been installed before we
        # check for its dependencies, in case it has a circular dependency on
        # itself
        processed_components[component.getName()] = component
        new_dependencies = {name:c for name,c in dependencies.items() if c and not name in processed_components}
        self.generate(builddir, component, new_dependencies, dependencies, recursive_deps, toplevel)

        logger.debug('recursive deps of %s:' % component)
        for d in recursive_deps.values():
            logger.debug('    %s' % d)

        processed_components.update(new_dependencies)
        for name, c in new_dependencies.items():
            for error in self.generateRecursive(c, all_components, os.path.join(builddir, 'ym', name), processed_components):
                yield error

    def checkStandardSourceDir(self, dirname, component):
        err = validate.sourceDirValidationError(dirname, component.getName())
        if err:
            logger.warn(err)

    def _sanitizeTarget(self, targetname):
        return re.sub('[^a-zA-Z0-9]', '_', targetname).upper()

    def _sanitizeSymbol(self, sym):
        return re.sub('[^a-zA-Z0-9]', '_', sym)

    def _listSubDirectories(self, component):
        ''' return: {
                manual: [list of subdirectories with manual CMakeLists],
                  auto: [list of pairs: (subdirectories name to autogenerate, a list of source files in that dir)],
                   bin: {dictionary of subdirectory name to binary name},
                  test: [list of directories that build tests]
              resource: [list of directories that contain resources]
            }
        '''
        manual_subdirs = []
        auto_subdirs = []
        bin_subdirs = {os.path.normpath(x) : y for x,y in component.getBinaries().items()};
        test_subdirs = []
        resource_subdirs = []
        for f in os.listdir(component.path):
            if f in Ignore_Subdirs or f.startswith('.') or f.startswith('_'):
                continue
            if os.path.isfile(os.path.join(component.path, f, 'CMakeLists.txt')):
                self.checkStandardSourceDir(f, component)
                # if the subdirectory has a CMakeLists.txt in it, then use that
                manual_subdirs.append(f)
                # tests only supported in the `test` directory for now
                if f in ('test',):
                    test_subdirs.append(f)
            elif f in ('source', 'test') or os.path.normpath(f) in bin_subdirs:
                # otherwise, if the directory has source files, generate a
                # CMakeLists in the corresponding temporary directory, and add
                # that.
                # For now we only do this for the source and test directories -
                # in theory we could do others
                sources = self.containsSourceFiles(os.path.join(component.path, f), component)
                if sources:
                    auto_subdirs.append((f, sources))
                    # tests only supported in the `test` directory for now
                    if f in ('test',):
                        test_subdirs.append(f)
            elif f in ('resource'):
                resource_subdirs.append(os.path.join(component.path, f))
            elif f.lower() in ('source', 'src', 'test', 'resource'):
                self.checkStandardSourceDir(f, component)
        return {
            "manual": manual_subdirs,
              "auto": auto_subdirs,
               "bin": bin_subdirs,
              "test": test_subdirs,
          "resource": resource_subdirs
        }

    def generate(self, builddir, component, active_dependencies, immediate_dependencies, all_dependencies, toplevel):
        ''' active_dependencies is the dictionary of components that need to be
            built for this component, but will not already have been built for
            another component.
        '''

        include_own_dir = string.Template(
            'include_directories("$path")\n'
        ).substitute(path=component.path)

        include_root_dirs = ''
        include_sys_dirs = ''
        include_other_dirs = ''
        objc_flags_set = {}
        objc_flags = []
        for name, c in itertools.chain(((component.getName(), component),), all_dependencies.items()):
            include_root_dirs += string.Template(
                'include_directories("$path")\n'
            ).substitute(path=escapeBackslash(c.path))
            dep_sys_include_dirs = c.getExtraSysIncludes()
            for d in dep_sys_include_dirs:
                include_sys_dirs += string.Template(
                    'include_directories(SYSTEM "$path")\n'
                ).substitute(path=escapeBackslash(os.path.join(c.path, d)))
            dep_extra_include_dirs = c.getExtraIncludes()
            for d in dep_extra_include_dirs:
                include_other_dirs += string.Template(
                    'include_directories("$path")\n'
                ).substitute(path=escapeBackslash(os.path.join(c.path, d)))
        for name, c in all_dependencies.items() + [(component.getName(), component)]:
            dep_extra_objc_flags = c.getExtraObjcFlags()
            # Try to warn Geraint when flags are clobbered. This will probably
            # miss some obscure flag forms, but it tries pretty hard
            for f in dep_extra_objc_flags:
                flag_name = None
                if len(f.split('=')) == 2:
                    flag_name = f.split('=')[0]
                elif f.startswith('-fno-'):
                    flag_name = f[5:]
                elif f.startswith('-fno'):
                    flag_name = f[4:]
                elif f.startswith('-f'):
                    flag_name = f[2:]
                if flag_name is not None:
                    if flag_name in objc_flags_set and objc_flags_set[flag_name] != name:
                        logger.warning(
                            'component %s Objective-C flag "%s" clobbers a value earlier set by component %s' % (
                            name, f, objc_flags_set[flag_name]
                        ))
                    objc_flags_set[flag_name] = name
                objc_flags.append(f)
        set_objc_flags = ' '.join(objc_flags)

        add_depend_subdirs = ''
        for name, c in active_dependencies.items():
            add_depend_subdirs += string.Template(
                'add_subdirectory("$subdir")\n'
            ).substitute(
                subdir=escapeBackslash(os.path.join(builddir, 'ym', name))
            )
        
        subdirs = self._listSubDirectories(component)
        manual_subdirs      = subdirs['manual']
        autogen_subdirs     = subdirs['auto']
        binary_subdirs      = subdirs['bin']
        test_subdirs        = subdirs['test']
        resource_subdirs    = subdirs['resource']

        add_own_subdirs = []
        for f in manual_subdirs:
            if os.path.isfile(os.path.join(component.path, f, 'CMakeLists.txt')):
                add_own_subdirs.append(
                    (os.path.join(component.path, f), os.path.join(builddir, f))
                )

        # names of all directories at this level with stuff in: used to figure
        # out what to link automatically
        all_subdirs = manual_subdirs + [x[0] for x in autogen_subdirs]
        for f, source_files in autogen_subdirs:
            if f in binary_subdirs:
                exe_name = binary_subdirs[f]
            else:
                exe_name = None
            if f in test_subdirs:
                self.generateTestDirList(
                    builddir, f, source_files, component, immediate_dependencies
                )
            else:
                self.generateSubDirList(
                    builddir, f, source_files, component, all_subdirs,
                    immediate_dependencies, exe_name, resource_subdirs
                )
            add_own_subdirs.append(
                (os.path.join(builddir, f), os.path.join(builddir, f))
            )
        
        # if we're not building anything other than tests, then we need to
        # generate a dummy library so that this component can still be linked
        # against
        if len(add_own_subdirs) <= len(test_subdirs):
            add_own_subdirs.append(self.createDummyLib(
                component, builddir, [x for x in immediate_dependencies]
            ))
        
        
        target_definitions = '-DTARGET=' + self._sanitizeTarget(self.target.getName())  + ' '
        set_targets_like = 'set(TARGET_LIKE_' + self._sanitizeTarget(self.target.getName()) + ' TRUE)\n'
        for target in self.target.dependencyResolutionOrder():
            if '*' not in target:
                target_definitions += '-DTARGET_LIKE_' + self._sanitizeTarget(target) + ' '
                set_targets_like += 'set(TARGET_LIKE_' + self._sanitizeTarget(target) + ' TRUE)\n'


        file_contents = str(Cheetah.Template.Template(CMakeLists_Template, searchList=[{
                            "toplevel": toplevel,
                         "target_name": self.target.getName(),
                    "set_targets_like": set_targets_like,
                      "toolchain_file": self.target.getToolchainFile(),
                      "component_name": component.getName(),
                     "include_own_dir": include_own_dir,
                   "include_root_dirs": include_root_dirs,
                    "include_sys_dirs": include_sys_dirs,
                  "include_other_dirs": include_other_dirs,
                      "set_objc_flags": set_objc_flags,
                  "add_depend_subdirs": add_depend_subdirs,
                     "add_own_subdirs": add_own_subdirs,
            "yotta_target_definitions": target_definitions,
                   "component_version": component.getVersion(),
                     "escapeBackslash": escapeBackslash
        }]))
        self._writeFile(os.path.join(builddir, 'CMakeLists.txt'), file_contents)

    def createDummyLib(self, component, builddir, link_dependencies):
        safe_name        = self._sanitizeSymbol(component.getName())
        dummy_dirname    = 'yotta_dummy_lib_%s' % safe_name
        dummy_cfile_name = 'dummy.c'
        logger.debug("create dummy lib: %s, %s, %s" % (safe_name, dummy_dirname, dummy_cfile_name))
        dummy_cmakelists = str(Cheetah.Template.Template(Dummy_CMakeLists_Template, searchList=[{
                   "cfile_name": dummy_cfile_name,
                      "libname": component.getName(),
            "link_dependencies": link_dependencies,
              "escapeBackslash": escapeBackslash
        }]))
        self._writeFile(os.path.join(builddir, dummy_dirname, "CMakeLists.txt"), dummy_cmakelists)
        dummy_cfile = "void __yotta_dummy_lib_symbol_%s(){}\n" % safe_name
        self._writeFile(os.path.join(builddir, dummy_dirname, dummy_cfile_name), dummy_cfile)
        return (os.path.join(builddir, dummy_dirname), os.path.join(builddir, dummy_dirname))

    def writeIfDifferent(self, fname, contents):
        try:
            with open(fname, "r+") as f:
                current_contents = f.read()
                if current_contents != contents: 
                    f.seek(0)
                    f.write(contents)
                    f.truncate()
        except IOError:
            with open(fname, "w") as f:
                f.write(contents)
    
    def generateTestDirList(self, builddir, dirname, source_files, component, immediate_dependencies):
        logger.debug('generate CMakeLists.txt for directory: %s' % os.path.join(component.path, dirname))

        link_dependencies = [x for x in immediate_dependencies]
        fname = os.path.join(builddir, dirname, 'CMakeLists.txt')
        
        # group the list of source files by subdirectory: generate one test for
        # each subdirectory, and one test for each file at the top level
        subdirs = defaultdict(list)
        toplevel_srcs = []
        for f in source_files:
            subrelpath = os.path.relpath(f.relpath, dirname)
            subdir = os.path.split(subrelpath)[0]
            if subdir:
                subdirs[subdir].append(f)
            else:
                toplevel_srcs.append(f)
        
        tests = []
        for f in toplevel_srcs:
            object_name = '%s-test-%s' % (
                component.getName(), os.path.basename(os.path.splitext(str(f))[0]).lower()
            )
            tests.append([[str(f)], object_name, [f.lang]])
        for subdirname, sources in subdirs.items():
            object_name = '%s-test-%s' % (
                component.getName(), subdirname.lower()
            )
            tests.append([[str(f) for f in sources], object_name, [f.lang for f in sources]])

        # link tests against the main executable
        link_dependencies.append(component.getName())
        file_contents = str(Cheetah.Template.Template(Test_CMakeLists_Template, searchList=[{
             'source_directory':os.path.join(component.path, dirname),
                        'tests':tests,
            'link_dependencies':link_dependencies,
              'escapeBackslash':escapeBackslash
        }]))

        self._writeFile(fname, file_contents)

    def generateSubDirList(self, builddir, dirname, source_files, component, all_subdirs, immediate_dependencies, executable_name, resource_subdirs):
        logger.debug('generate CMakeLists.txt for directory: %s' % os.path.join(component.path, dirname))

        link_dependencies = [x for x in immediate_dependencies]
        fname = os.path.join(builddir, dirname, 'CMakeLists.txt')

        if dirname == 'source' or executable_name:
            if executable_name:
                object_name = executable_name
                executable  = True
            else:
                object_name = component.getName()
                executable  = False
            # if we're building the main library, or an executable for this
            # component, then we should link against all the other directories
            # containing cmakelists:
            link_dependencies += [x for x in all_subdirs if x not in ('source', 'test', dirname)]

            # Find resource files
            resource_files = []
            for f in resource_subdirs:
                for root, dires, files in os.walk(f):
                    for f in files:
                        resource_files.append(os.path.join(root, f))
            
            file_contents = str(Cheetah.Template.Template(Subdir_CMakeLists_Template, searchList=[{
                    'source_directory': os.path.join(component.path, dirname),
                          'executable': executable,
                          'file_names': [str(f) for f in source_files],
                         'object_name': object_name,
                   'link_dependencies': link_dependencies,
                           'languages': set(f.lang for f in source_files),
                        'source_files': set((f.fullpath, f.lang) for f in source_files),
                      "resource_files": resource_files,
                     'escapeBackslash': escapeBackslash
            }]))
        else:
            raise Exception('auto CMakeLists for non-source/test directories is not supported')
        self._writeFile(fname, file_contents)


    def containsSourceFiles(self, directory, component):
        c_exts          = set(('.c',))
        cpp_exts        = set(('.cpp','.cc','.cxx'))
        objc_exts       = set(('.m', '.mm'))
        header_exts     = set(('.h',))
        plist_exts      = set(('.plist',))
        
        sources = []
        for root, dires, files in os.walk(directory):
            for f in files:
                name, ext = os.path.splitext(f)
                ext = ext.lower()
                fullpath = os.path.join(root, f)
                relpath  = os.path.relpath(fullpath, component.path)
                if component.ignores(relpath):
                    continue
                if ext in c_exts:
                    sources.append(SourceFile(fullpath, relpath, 'c'))
                elif ext in cpp_exts:
                    sources.append(SourceFile(fullpath, relpath, 'cpp'))
                elif ext in objc_exts:
                    sources.append(SourceFile(fullpath, relpath, 'objc'))
                elif ext in header_exts:
                    sources.append(SourceFile(fullpath, relpath, 'header'))
                elif ext in plist_exts:
                    sources.append(SourceFile(fullpath, relpath, 'plist'))
        return sources

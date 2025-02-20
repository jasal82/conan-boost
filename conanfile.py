from conans import ConanFile
from conans import tools
from conans.client.build.cppstd_flags import cppstd_flag
from conans.model.version import Version
from conans.errors import ConanException

import os
import sys
import shutil

try:
    from cStringIO import StringIO
except ImportError:
    from io import StringIO

# From from *1 (see below, b2 --show-libraries), also ordered following linkage order
# see https://github.com/Kitware/CMake/blob/master/Modules/FindBoost.cmake to know the order


lib_list = ['math', 'wave', 'container', 'contract', 'exception', 'graph', 'iostreams', 'locale', 'log',
            'program_options', 'random', 'regex', 'mpi', 'serialization',
            'coroutine', 'fiber', 'context', 'timer', 'thread', 'chrono', 'date_time',
            'atomic', 'filesystem', 'system', 'graph_parallel', 'python',
            'stacktrace', 'test', 'type_erasure']


class BoostConan(ConanFile):
    name = "boost"
    version = "1.70.0"
    settings = "os", "arch", "compiler", "build_type"
    folder_name = "boost_%s" % version.replace(".", "_")
    description = "Boost provides free peer-reviewed portable C++ source libraries"
    # The current python option requires the package to be built locally, to find default Python
    # implementation
    options = {
        "shared": [True, False],
        "header_only": [True, False],
        "error_code_header_only": [True, False],
        "system_no_deprecated": [True, False],
        "asio_no_deprecated": [True, False],
        "filesystem_no_deprecated": [True, False],
        "fPIC": [True, False],
        "layout": ["system", "versioned", "tagged"],
        "magic_autolink": [True, False],  # enables BOOST_ALL_NO_LIB
        "python_executable": "ANY",  # system default python installation is used, if None
        "python_version": "ANY",  # major.minor; computed automatically, if None
        "namespace": "ANY",  # custom boost namespace for bcp, e.g. myboost
        "namespace_alias": [True, False],  # enable namespace alias for bcp, boost=myboost
        "zlib": [True, False],
        "bzip2": [True, False],
        "lzma": [True, False],
        "zstd": [True, False]
    }
    options.update({"without_%s" % libname: [True, False] for libname in lib_list})

    default_options = ["shared=False",
                       "header_only=False",
                       "error_code_header_only=False",
                       "system_no_deprecated=False",
                       "asio_no_deprecated=False",
                       "filesystem_no_deprecated=False",
                       "fPIC=True",
                       "layout=system",
                       "magic_autolink=False",
                       "python_executable=None",
                       "python_version=None",
                       "namespace=boost",
                       "namespace_alias=False",
                       "zlib=True",
                       "bzip2=True",
                       "lzma=False",
                       "zstd=False"]

    default_options.extend(["without_%s=False" % libname for libname in lib_list if libname != "python"])
    default_options.append("without_python=True")
    default_options = tuple(default_options)

    url = "https://github.com/lasote/conan-boost"
    license = "Boost Software License - Version 1.0. http://www.boost.org/LICENSE_1_0.txt"
    short_paths = True
    no_copy_source = True

    exports_sources = ['patches/*']

    _bcp_dir = "custom-boost"

    def config_options(self):
        if self.settings.os == "Windows":
            self.options.remove("fPIC")

    @property
    def _is_msvc(self):
        return self.settings.compiler == "Visual Studio"

    @property
    def zip_bzip2_requires_needed(self):
        return not self.options.without_iostreams and not self.options.header_only

    def configure(self):
        if self.zip_bzip2_requires_needed:
            if self.options.zlib:
                self.requires("zlib/1.2.11@conan/stable")
            if self.options.bzip2:
                self.requires("bzip2/1.0.6@conan/stable")
            if self.options.lzma:
                self.requires("lzma/5.2.4@bincrafters/stable")
            if self.options.zstd:
                self.requires("zstd/1.3.5@bincrafters/stable")

    def package_id(self):
        if self.options.header_only:
            self.info.header_only()
            self.info.options.header_only = True
        else:
            del self.info.options.python_executable  # PATH to the interpreter is not important, only version matters
            if self.options.without_python:
                del self.info.options.python_version
            else:
                self.info.options.python_version = self._python_version

    def source(self):
        if tools.os_info.is_windows:
            sha256 = "48f379b2e90dd1084429aae87d6bdbde9670139fa7569ee856c8c86dd366039d"
            extension = ".zip"
        else:
            sha256 = "430ae8354789de4fd19ee52f3b1f739e1fba576f0aded0897c3c2bc00fb38778"
            extension = ".tar.bz2"

        zip_name = "%s%s" % (self.folder_name, extension)
        url = "https://dl.bintray.com/boostorg/release/%s/source/%s" % (self.version, zip_name)
        tools.get(url, sha256=sha256)

        for patch in ["python_base_prefix.patch", "boost_build_asmflags.patch"]:
            tools.patch(patch_file=os.path.join("patches", patch),
                        base_path=os.path.join(self.source_folder, self.folder_name))


    ##################### BUILDING METHODS ###########################

    @property
    def _python_executable(self):
        """
        obtain full path to the python interpreter executable
        :return: path to the python interpreter executable, either set by option, or system default
        """
        exe = self.options.python_executable if self.options.python_executable else sys.executable
        return str(exe).replace('\\', '/')

    def _run_python_script(self, script):
        """
        execute python one-liner script and return its output
        :param script: string containing python script to be executed
        :return: output of the python script execution, or None, if script has failed
        """
        output = StringIO()
        command = '"%s" -c "%s"' % (self._python_executable, script)
        self.output.info('running %s' % command)
        try:
            self.run(command=command, output=output)
        except ConanException:
            self.output.info("(failed)")
            return None
        output = output.getvalue().strip()
        self.output.info(output)
        return output if output != "None" else None

    def _get_python_path(self, name):
        """
        obtain path entry for the python installation
        :param name: name of the python config entry for path to be queried (such as "include", "platinclude", etc.)
        :return: path entry from the sysconfig
        """
        # https://docs.python.org/3/library/sysconfig.html
        # https://docs.python.org/2.7/library/sysconfig.html
        return self._run_python_script("from __future__ import print_function; "
                                       "import sysconfig; "
                                       "print(sysconfig.get_path('%s'))" % name)

    def _get_python_sc_var(self, name):
        """
        obtain value of python sysconfig variable
        :param name: name of variable to be queried (such as LIBRARY or LDLIBRARY)
        :return: value of python sysconfig variable
        """
        return self._run_python_script("from __future__ import print_function; "
                                       "import sysconfig; "
                                       "print(sysconfig.get_config_var('%s'))" % name)

    def _get_python_du_var(self, name):
        """
        obtain value of python distutils sysconfig variable
        (sometimes sysconfig returns empty values, while python.sysconfig provides correct values)
        :param name: name of variable to be queried (such as LIBRARY or LDLIBRARY)
        :return: value of python sysconfig variable
        """
        return self._run_python_script("from __future__ import print_function; "
                                       "import distutils.sysconfig as du_sysconfig; "
                                       "print(du_sysconfig.get_config_var('%s'))" % name)

    def _get_python_var(self, name):
        """
        obtain value of python variable, either by sysconfig, or by distutils.sysconfig
        :param name: name of variable to be queried (such as LIBRARY or LDLIBRARY)
        :return: value of python sysconfig variable
        """
        return self._get_python_sc_var(name) or self._get_python_du_var(name)

    @property
    def _python_version(self):
        """
        obtain version of python interpreter
        :return: python interpreter version, in format major.minor
        """
        version = self._run_python_script("from __future__ import print_function; "
                                          "import sys; "
                                          "print('%s.%s' % (sys.version_info[0], sys.version_info[1]))")
        if self.options.python_version and version != self.options.python_version:
            raise Exception("detected python version %s doesn't match conan option %s" % (version,
                                                                                          self.options.python_version))
        return version

    @property
    def _python_inc(self):
        """
        obtain the result of the "sysconfig.get_python_inc()" call
        :return: result of the "sysconfig.get_python_inc()" execution
        """
        return self._run_python_script("from __future__ import print_function; "
                                       "import sysconfig; "
                                       "print(sysconfig.get_python_inc())")

    @property
    def _python_abiflags(self):
        """
        obtain python ABI flags, see https://www.python.org/dev/peps/pep-3149/ for the details
        :return: the value of python ABI flags
        """
        return self._run_python_script("from __future__ import print_function; "
                                       "import sys; "
                                       "print(getattr(sys, 'abiflags', ''))")

    @property
    def _python_includes(self):
        """
        attempt to find directory containing Python.h header file
        :return: the directory with python includes
        """
        include = self._get_python_path('include')
        plat_include = self._get_python_path('platinclude')
        include_py = self._get_python_var('INCLUDEPY')
        include_dir = self._get_python_var('INCLUDEDIR')
        python_inc = self._python_inc

        candidates = [include,
                      plat_include,
                      include_py,
                      include_dir,
                      python_inc]
        for candidate in candidates:
            if candidate:
                python_h = os.path.join(candidate, 'Python.h')
                self.output.info('checking %s' % python_h)
                if os.path.isfile(python_h):
                    self.output.info('found Python.h: %s' % python_h)
                    return candidate.replace('\\', '/')
        raise Exception("couldn't locate Python.h - make sure you have installed python development files")

    @property
    def _python_libraries(self):
        """
        attempt to find python development library
        :return: the full path to the python library to be linked with
        """
        library = self._get_python_var("LIBRARY")
        ldlibrary = self._get_python_var("LDLIBRARY")
        libdir = self._get_python_var("LIBDIR")
        multiarch = self._get_python_var("MULTIARCH")
        masd = self._get_python_var("multiarchsubdir")
        with_dyld = self._get_python_var("WITH_DYLD")
        if libdir and multiarch and masd:
            if masd.startswith(os.sep):
                masd = masd[len(os.sep):]
            libdir = os.path.join(libdir, masd)

        if not libdir:
            libdest = self._get_python_var("LIBDEST")
            libdir = os.path.join(os.path.dirname(libdest), "libs")

        candidates = [ldlibrary, library]
        library_prefixes = [""] if self._is_msvc else ["", "lib"]
        library_suffixes = [".lib"] if self._is_msvc else [".so", ".dll.a", ".a"]
        if with_dyld:
            library_suffixes.insert(0, ".dylib")

        python_version = self._python_version
        python_version_no_dot = python_version.replace(".", "")
        versions = ["", python_version, python_version_no_dot]
        abiflags = self._python_abiflags

        for prefix in library_prefixes:
            for suffix in library_suffixes:
                for version in versions:
                    candidates.append("%spython%s%s%s" % (prefix, version, abiflags, suffix))

        for candidate in candidates:
            if candidate:
                python_lib = os.path.join(libdir, candidate)
                self.output.info('checking %s' % python_lib)
                if os.path.isfile(python_lib):
                    self.output.info('found python library: %s' % python_lib)
                    return python_lib.replace('\\', '/')
        raise Exception("couldn't locate python libraries - make sure you have installed python development files")

    def _clean(self):
        src = os.path.join(self.source_folder, self.folder_name)
        clean_dirs = [os.path.join(self.build_folder, "bin.v2"),
                      os.path.join(self.build_folder, "architecture"),
                      os.path.join(self.source_folder, self._bcp_dir),
                      os.path.join(src, "dist", "bin"),
                      os.path.join(src, "stage"),
                      os.path.join(src, "tools", "build", "src", "engine", "bootstrap"),
                      os.path.join(src, "tools", "build", "src", "engine", "bin.ntx86"),
                      os.path.join(src, "tools", "build", "src", "engine", "bin.ntx86_64")]
        for d in clean_dirs:
            if os.path.isdir(d):
                self.output.warn('removing "%s"' % d)
                shutil.rmtree(d)

    @property
    def _b2_exe(self):
        folder = os.path.join(self.source_folder, self.folder_name, "tools", "build")
        return os.path.join(folder, "b2.exe" if tools.os_info.is_windows else "b2")

    @property
    def _bcp_exe(self):
        folder = os.path.join(self.source_folder, self.folder_name, "dist", "bin")
        return os.path.join(folder, "bcp.exe" if tools.os_info.is_windows else "bcp")

    @property
    def _use_bcp(self):
        return self.options.namespace != "boost"

    @property
    def _boost_dir(self):
        return self._bcp_dir if self._use_bcp else self.folder_name

    @property
    def _boost_build_dir(self):
        return os.path.join(self.source_folder, self.folder_name, "tools", "build")

    def _build_bcp(self):
        folder = os.path.join(self.source_folder, self.folder_name, 'tools', 'bcp')
        with tools.vcvars(self.settings) if self._is_msvc else tools.no_op():
            with tools.chdir(folder):
                command = "%s -j%s --abbreviate-paths -d2" % (self._b2_exe, tools.cpu_count())
                self.output.warn(command)
                self.run(command)

    def _run_bcp(self):
        with tools.vcvars(self.settings) if self._is_msvc else tools.no_op():
            with tools.chdir(self.source_folder):
                os.mkdir(self._bcp_dir)
                namespace = "--namespace=%s" % self.options.namespace
                alias = "--namespace-alias" if self.options.namespace_alias else ""
                boostdir = "--boost=%s" % self.folder_name
                libraries = {"build", "boost-build.jam", "boostcpp.jam"}
                for d in os.listdir(os.path.join(self.folder_name, "boost")):
                    if os.path.isdir(os.path.join(self.folder_name, "boost", d)):
                        libraries.add(d)
                for d in os.listdir(os.path.join(self.folder_name, "libs")):
                    if os.path.isdir(os.path.join(self.folder_name, "libs", d)):
                        libraries.add(d)
                libraries = ' '.join(libraries)
                command = "{bcp} {namespace} {alias} " \
                          "{boostdir} {libraries} {outdir}".format(bcp=self._bcp_exe,
                                                                   namespace=namespace,
                                                                   alias=alias,
                                                                   libraries=libraries,
                                                                   boostdir=boostdir,
                                                                   outdir=self._bcp_dir)
                self.output.warn(command)
                self.run(command)

    def build(self):
        if self.options.header_only:
            self.output.warn("Header only package, skipping build")
            return

        self._clean()
        self._bootstrap()

        if self._use_bcp:
            self._build_bcp()
            self._run_bcp()

        flags = self.get_build_flags()
        # Help locating bzip2 and zlib
        self.create_user_config_jam(self._boost_build_dir)

        # JOIN ALL FLAGS
        b2_flags = " ".join(flags)
        full_command = "%s %s -j%s --abbreviate-paths -d2" % (self._b2_exe, b2_flags, tools.cpu_count())
        # -d2 is to print more debug info and avoid travis timing out without output
        sources = os.path.join(self.source_folder, self._boost_dir)
        full_command += ' --debug-configuration --build-dir="%s"' % self.build_folder
        self.output.warn(full_command)

        with tools.vcvars(self.settings) if self._is_msvc else tools.no_op():
            with tools.chdir(sources):
                # to locate user config jam (BOOST_BUILD_PATH)
                with tools.environment_append({"BOOST_BUILD_PATH": self._boost_build_dir}):
                    # To show the libraries *1
                    # self.run("%s --show-libraries" % b2_exe)
                    self.run(full_command)

    @property
    def _b2_os(self):
        return {"Windows": "windows",
                "WindowsStore": "windows",
                "Linux": "linux",
                "Android": "android",
                "Macos": "darwin",
                "iOS": "iphone",
                "watchOS": "iphone",
                "tvOS": "appletv",
                "FreeBSD": "freebsd",
                "SunOS": "solatis"}.get(str(self.settings.os))

    @property
    def _b2_address_model(self):
        if str(self.settings.arch) in ["x86_64", "ppc64", "ppc64le", "mips64", "armv8", "sparcv9"]:
            return "64"
        else:
            return "32"

    @property
    def _b2_binary_format(self):
        return {"Windows": "pe",
                "WindowsStore": "pe",
                "Linux": "elf",
                "Android": "elf",
                "Macos": "mach-o",
                "iOS": "mach-o",
                "watchOS": "mach-o",
                "tvOS": "mach-o",
                "FreeBSD": "elf",
                "SunOS": "elf"}.get(str(self.settings.os))

    @property
    def _b2_architecture(self):
        if str(self.settings.arch).startswith('x86'):
            return 'x86'
        elif str(self.settings.arch).startswith('ppc'):
            return 'power'
        elif str(self.settings.arch).startswith('arm'):
            return 'arm'
        elif str(self.settings.arch).startswith('sparc'):
            return 'sparc'
        elif str(self.settings.arch).startswith('mips64'):
            return 'mips64'
        elif str(self.settings.arch).startswith('mips'):
            return 'mips1'
        else:
            return None

    @property
    def _b2_abi(self):
        if str(self.settings.arch).startswith('x86'):
            return "ms" if str(self.settings.os) in ["Windows", "WindowsStore"] else "sysv"
        elif str(self.settings.arch).startswith('ppc'):
            return "sysv"
        elif str(self.settings.arch).startswith('arm'):
            return "aapcs"
        elif str(self.settings.arch).startswith('mips'):
            return "o32"
        else:
            return None

    def get_build_flags(self):

        if tools.cross_building(self.settings):
            flags = self.get_build_cross_flags()
        else:
            flags = []

        # https://www.boost.org/doc/libs/1_70_0/libs/context/doc/html/context/architectures.html
        if self._b2_os:
            flags.append("target-os=%s" % self._b2_os)
        if self._b2_architecture:
            flags.append("architecture=%s" % self._b2_architecture)
        if self._b2_address_model:
            flags.append("address-model=%s" % self._b2_address_model)
        if self._b2_binary_format:
            flags.append("binary-format=%s" % self._b2_binary_format)
        if self._b2_abi:
            flags.append("abi=%s" % self._b2_abi)

        flags.append("--layout=%s" % self.options.layout)
        flags.append("-sBOOST_BUILD_PATH=%s" % self._boost_build_dir)
        flags.append("-sNO_ZLIB=%s" % ("0" if self.options.zlib else "1"))
        flags.append("-sNO_BZIP2=%s" % ("0" if self.options.bzip2 else "1"))
        flags.append("-sNO_LZMA=%s" % ("0" if self.options.lzma else "1"))
        flags.append("-sNO_ZSTD=%s" % ("0" if self.options.zstd else "1"))

        def add_defines(option, library):
            if option:
                for define in self.deps_cpp_info[library].defines:
                    flags.append("define=%s" % define)

        if self.zip_bzip2_requires_needed:
            add_defines(self.options.zlib, "zlib")
            add_defines(self.options.bzip2, "bzip2")
            add_defines(self.options.lzma, "lzma")
            add_defines(self.options.zstd, "zstd")

        if self._is_msvc and self.settings.compiler.runtime:
            flags.append("runtime-link=%s" % ("static" if "MT" in str(self.settings.compiler.runtime) else "shared"))

        flags.append("threading=multi")

        flags.append("link=%s" % ("static" if not self.options.shared else "shared"))
        if self.settings.build_type == "Debug":
            flags.append("variant=debug")
        else:
            flags.append("variant=release")

        for libname in lib_list:
            if getattr(self.options, "without_%s" % libname):
                flags.append("--without-%s" % libname)

        toolset, _, _ = self.get_toolset_version_and_exe()
        flags.append("toolset=%s" % toolset)

        if self.settings.get_safe("compiler.cppstd"):
            flags.append("cxxflags=%s" % cppstd_flag(
                    self.settings.get_safe("compiler"),
                    self.settings.get_safe("compiler.version"),
                    self.settings.get_safe("compiler.cppstd")
                )
            )

        # CXX FLAGS
        cxx_flags = []
        # fPIC DEFINITION
        if self.settings.os != "Windows":
            if self.options.fPIC:
                cxx_flags.append("-fPIC")

        # Standalone toolchain fails when declare the std lib
        if self.settings.os != "Android":
            try:
                if str(self.settings.compiler.libcxx) == "libstdc++":
                    flags.append("define=_GLIBCXX_USE_CXX11_ABI=0")
                elif str(self.settings.compiler.libcxx) == "libstdc++11":
                    flags.append("define=_GLIBCXX_USE_CXX11_ABI=1")
                if "clang" in str(self.settings.compiler):
                    if str(self.settings.compiler.libcxx) == "libc++":
                        cxx_flags.append("-stdlib=libc++")
                        flags.append('linkflags="-stdlib=libc++"')
                    else:
                        cxx_flags.append("-stdlib=libstdc++")
            except:
                pass

        if self.options.error_code_header_only:
            flags.append("define=BOOST_ERROR_CODE_HEADER_ONLY=1")
        if self.options.system_no_deprecated:
            flags.append("define=BOOST_SYSTEM_NO_DEPRECATED=1")
        if self.options.asio_no_deprecated:
            flags.append("define=BOOST_ASIO_NO_DEPRECATED=1")
        if self.options.filesystem_no_deprecated:
            flags.append("define=BOOST_FILESYSTEM_NO_DEPRECATED=1")

        if tools.is_apple_os(self.settings.os):
            if self.settings.get_safe("os.version"):
                cxx_flags.append(tools.apple_deployment_target_flag(self.settings.os,
                                                                    self.settings.os.version))

        if self.settings.os == "iOS":
            cxx_flags.append("-DBOOST_AC_USE_PTHREADS")
            cxx_flags.append("-DBOOST_SP_USE_PTHREADS")
            cxx_flags.append("-fvisibility=hidden")
            cxx_flags.append("-fvisibility-inlines-hidden")
            cxx_flags.append("-fembed-bitcode")

        cxx_flags = 'cxxflags="%s"' % " ".join(cxx_flags) if cxx_flags else ""
        flags.append(cxx_flags)

        return flags

    def get_build_cross_flags(self):
        arch = self.settings.get_safe('arch')
        flags = []
        self.output.info("Cross building, detecting compiler...")

        if arch.startswith('arm'):
            if 'hf' in arch:
                flags.append('-mfloat-abi=hard')
        elif arch in ["x86", "x86_64"]:
            pass
        elif arch.startswith("ppc"):
            pass
        else:
            raise Exception("I'm so sorry! I don't know the appropriate ABI for "
                            "your architecture. :'(")
        self.output.info("Cross building flags: %s" % flags)

        return flags

    @property
    def _ar(self):
        if "AR" in os.environ:
            return os.environ["AR"]
        if tools.is_apple_os(self.settings.os) and self.settings.compiler == "apple-clang":
            return tools.XCRun(self.settings).ar
        return None

    @property
    def _ranlib(self):
        if "RANLIB" in os.environ:
            return os.environ["RANLIB"]
        if tools.is_apple_os(self.settings.os) and self.settings.compiler == "apple-clang":
            return tools.XCRun(self.settings).ranlib
        return None

    @property
    def _cxx(self):
        if "CXX" in os.environ:
            return os.environ["CXX"]
        if tools.is_apple_os(self.settings.os) and self.settings.compiler == "apple-clang":
            return tools.XCRun(self.settings).cxx
        return None

    def create_user_config_jam(self, folder):
        """To help locating the zlib and bzip2 deps"""
        self.output.warn("Patching user-config.jam")

        compiler_command = self._cxx

        contents = ""
        if self.zip_bzip2_requires_needed:
            def create_library_config(name):
                reference = str(self.requires[name])
                version_name = reference.split("@")[0]
                version = version_name.split("/")[1]
                includedir = self.deps_cpp_info[name].include_paths[0].replace('\\', '/')
                libdir = self.deps_cpp_info[name].lib_paths[0].replace('\\', '/')
                lib = self.deps_cpp_info[name].libs[0]
                return "\nusing {name} : {version} : " \
                       "<include>{includedir} " \
                       "<search>{libdir} " \
                       "<name>{lib} ;".format(name=name,
                                              version=version,
                                              includedir=includedir,
                                              libdir=libdir,
                                              lib=lib)

            contents = ""
            if self.options.zlib:
                contents += create_library_config("zlib")
            if self.options.bzip2:
                contents += create_library_config("bzip2")
            if self.options.lzma:
                contents += create_library_config("lzma")
            if self.options.zstd:
                contents += create_library_config("zstd")

        if not self.options.without_python:
            # https://www.boost.org/doc/libs/1_70_0/libs/python/doc/html/building/configuring_boost_build.html
            contents += "\nusing python : {version} : {executable} : {includes} :  {libraries} ;"\
                .format(version=self._python_version,
                        executable=self._python_executable,
                        includes=self._python_includes,
                        libraries=self._python_libraries)

        toolset, version, exe = self.get_toolset_version_and_exe()
        exe = compiler_command or exe  # Prioritize CXX

        # Specify here the toolset with the binary if present if don't empty parameter : :
        contents += '\nusing "%s" : "%s" : ' % (toolset, version)
        contents += ' %s' % exe.replace("\\", "/")

        if tools.is_apple_os(self.settings.os):
            if self.settings.compiler == "apple-clang":
                contents += " -isysroot %s" % tools.XCRun(self.settings).sdk_path
            if self.settings.get_safe("arch"):
                contents += " -arch %s" % tools.to_apple_arch(self.settings.arch)

        contents += " : \n"
        if self._ar:
            contents += '<archiver>"%s" ' % tools.which(self._ar).replace("\\", "/")
        if self._ranlib:
            contents += '<ranlib>"%s" ' % tools.which(self._ranlib).replace("\\", "/")
        if "CXXFLAGS" in os.environ:
            contents += '<cxxflags>"%s" ' % os.environ["CXXFLAGS"]
        if "CFLAGS" in os.environ:
            contents += '<cflags>"%s" ' % os.environ["CFLAGS"]
        if "LDFLAGS" in os.environ:
            contents += '<linkflags>"%s" ' % os.environ["LDFLAGS"]
        if "ASFLAGS" in os.environ:
            contents += '<asmflags>"%s" ' % os.environ["ASFLAGS"]

        contents += " ;"

        self.output.warn(contents)
        filename = "%s/user-config.jam" % folder
        tools.save(filename,  contents)

    def get_toolset_version_and_exe(self):
        compiler_version = str(self.settings.compiler.version)
        compiler = str(self.settings.compiler)
        if self._is_msvc:
            cversion = self.settings.compiler.version
            _msvc_version = "14.1" if Version(str(cversion)) >= "15" else "%s.0" % cversion
            return "msvc", _msvc_version, ""
        elif self.settings.os == "Windows" and self.settings.compiler == "clang":
            return "clang-win", compiler_version, ""
        elif self.settings.compiler == "gcc" and tools.is_apple_os(self.settings.os):
            return "darwin", compiler_version, self._cxx
        elif compiler == "gcc" and compiler_version[0] >= "5":
            # For GCC >= v5 we only need the major otherwise Boost doesn't find the compiler
            # The NOT windows check is necessary to exclude MinGW:
            if not tools.which("g++-%s" % compiler_version[0]):
                # In fedora 24, 25 the gcc is 6, but there is no g++-6 and the detection is 6.3.1
                # so b2 fails because 6 != 6.3.1. Specify the exe to avoid the smart detection
                executable = tools.which("g++") or ""
            else:
                executable = ""
            return compiler, compiler_version[0], executable
        elif self.settings.compiler == "apple-clang":
            return "clang-darwin", compiler_version, self._cxx
        elif self.settings.os == "Android" and self.settings.compiler == "clang":
            return "clang-linux", compiler_version, self._cxx
        elif str(self.settings.compiler) in ["clang", "gcc"]:
            # For GCC < v5 and Clang we need to provide the entire version string
            return compiler, compiler_version, ""
        elif self.settings.compiler == "sun-cc":
            return "sunpro", compiler_version, ""
        else:
            return compiler, compiler_version, ""

    ##################### BOOSTRAP METHODS ###########################
    def _get_boostrap_toolset(self):
        if self._is_msvc:
            comp_ver = self.settings.compiler.version
            return "vc%s" % ("141" if Version(str(comp_ver)) >= "15" else comp_ver)

        if tools.os_info.is_windows:
            return "gcc" if self.settings.compiler == "gcc" else ""

        if tools.os_info.is_macos:
            return "darwin"

        with_toolset = {"apple-clang": "darwin"}.get(str(self.settings.compiler),
                                                     str(self.settings.compiler))

        # fallback for the case when no unversioned gcc/clang is available
        if with_toolset in ["gcc", "clang"] and not tools.which(with_toolset):
            with_toolset = "cc"
        return with_toolset

    def _bootstrap(self):
        folder = os.path.join(self.source_folder, self.folder_name, "tools", "build")
        try:
            bootstrap = "bootstrap.bat" if tools.os_info.is_windows else "./bootstrap.sh"
            with tools.vcvars(self.settings) if self._is_msvc else tools.no_op():
                with tools.chdir(folder):
                    if tools.cross_building(self.settings):
                        cmd = bootstrap
                    else:
                        option = "" if tools.os_info.is_windows else "-with-toolset="
                        cmd = "%s %s%s" % (bootstrap, option, self._get_boostrap_toolset())
                    self.output.info(cmd)
                    with tools.environment_append({"CC": None, "CXX": None, "CFLAGS": None, "CXXFLAGS": None}):
                        self.run(cmd)

        except Exception as exc:
            self.output.warn(str(exc))
            if os.path.exists(os.path.join(folder, "bootstrap.log")):
                self.output.warn(tools.load(os.path.join(folder, "bootstrap.log")))
            raise

    ####################################################################

    def package(self):
        # This stage/lib is in source_folder... Face palm, looks like it builds in build but then
        # copy to source with the good lib name
        out_lib_dir = os.path.join(self._boost_dir, "stage", "lib")
        self.copy(pattern="*", dst="include/boost", src="%s/boost" % self._boost_dir)
        if not self.options.shared:
            self.copy(pattern="*.a", dst="lib", src=out_lib_dir, keep_path=False)
        self.copy(pattern="*.so", dst="lib", src=out_lib_dir, keep_path=False, symlinks=True)
        self.copy(pattern="*.so.*", dst="lib", src=out_lib_dir, keep_path=False, symlinks=True)
        self.copy(pattern="*.dylib*", dst="lib", src=out_lib_dir, keep_path=False)
        self.copy(pattern="*.lib", dst="lib", src=out_lib_dir, keep_path=False)
        self.copy(pattern="*.dll", dst="bin", src=out_lib_dir, keep_path=False)

        # When first call with source do not package anything
        if not os.path.exists(os.path.join(self.package_folder, "lib")):
            return

    def package_info(self):
        gen_libs = [] if self.options.header_only else tools.collect_libs(self)

        # List of lists, so if more than one matches the lib like serialization and wserialization
        # both will be added to the list
        ordered_libs = [[] for _ in range(len(lib_list))]

        # The order is important, reorder following the lib_list order
        missing_order_info = []
        for real_lib_name in gen_libs:
            for pos, alib in enumerate(lib_list):
                if os.path.splitext(real_lib_name)[0].split("-")[0].endswith(alib):
                    ordered_libs[pos].append(real_lib_name)
                    break
            else:
                # self.output.info("Missing in order: %s" % real_lib_name)
                if "_exec_monitor" not in real_lib_name:  # https://github.com/bincrafters/community/issues/94
                    missing_order_info.append(real_lib_name)  # Assume they do not depend on other

        # Flat the list and append the missing order
        self.cpp_info.libs = [item for sublist in ordered_libs
                                      for item in sublist if sublist] + missing_order_info

        if self.options.without_test:  # remove boost_unit_test_framework
            self.cpp_info.libs = [lib for lib in self.cpp_info.libs if "unit_test" not in lib]

        self.output.info("LIBRARIES: %s" % self.cpp_info.libs)
        self.output.info("Package folder: %s" % self.package_folder)

        if not self.options.header_only and self.options.shared:
            self.cpp_info.defines.append("BOOST_ALL_DYN_LINK")

        if self.options.system_no_deprecated:
            self.cpp_info.defines.append("BOOST_SYSTEM_NO_DEPRECATED")

        if self.options.asio_no_deprecated:
            self.cpp_info.defines.append("BOOST_ASIO_NO_DEPRECATED")

        if self.options.filesystem_no_deprecated:
            self.cpp_info.defines.append("BOOST_FILESYSTEM_NO_DEPRECATED")

        if not self.options.header_only:
            if self.options.error_code_header_only:
                self.cpp_info.defines.append("BOOST_ERROR_CODE_HEADER_ONLY")

            if not self.options.without_python:
                if not self.options.shared:
                    self.cpp_info.defines.append("BOOST_PYTHON_STATIC_LIB")

            if self._is_msvc:
                if not self.options.magic_autolink:
                    # DISABLES AUTO LINKING! NO SMART AND MAGIC DECISIONS THANKS!
                    self.cpp_info.defines.append("BOOST_ALL_NO_LIB")
                    self.output.info("Disabled magic autolinking (smart and magic decisions)")
                else:
                    if self.options.layout == "system":
                        self.cpp_info.defines.append("BOOST_AUTO_LINK_SYSTEM")
                    elif self.options.layout == "tagged":
                        self.cpp_info.defines.append("BOOST_AUTO_LINK_TAGGED")
                    self.output.info("Enabled magic autolinking (smart and magic decisions)")

                # https://github.com/conan-community/conan-boost/issues/127#issuecomment-404750974
                self.cpp_info.libs.append("bcrypt")
            elif self.settings.os == "Linux":
                # https://github.com/conan-community/conan-boost/issues/135
                self.cpp_info.libs.append("pthread")

        self.env_info.BOOST_ROOT = self.package_folder

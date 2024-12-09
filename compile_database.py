from collections import defaultdict
import logging
import os
import re
import subprocess
import glob
from typing import Dict, List, Optional


globalStore = {}

cmd_split_pattern = re.compile(
    r"""
"((?:[^"]|(?<=\\)")*)" |     # like "xxx xxx", allow \"
'([^']*)' |     # like 'xxx xxx'
((?:\\[ ]|\S)+) # like xxx\ xxx
""",
    re.X,
)

logger = logging.getLogger(__name__)

def isProjectRoot(directory):
    # Check if there are any .xcodeproj files in the directory
    xcodeproj_files = glob.glob(os.path.join(directory, "*.xcodeproj"))
    if xcodeproj_files:
        return True  # Project root identified by .xcodeproj files

    # Fallback: Check for .git directory
    return os.path.exists(os.path.join(directory, ".git"))


def additionalFlags(flagsPath):
    if flagsPath and os.path.isfile(flagsPath):

        def valid(s):
            return s and not s.startswith("#")

        with open(flagsPath) as f:
            return list(filter(valid, (line.strip() for line in f)))
    return []


def findAllHeaderDirectory(rootDirectory, store):
    headerDirsCacheDict = store.setdefault("headerDirs", {})
    headerDirs = headerDirsCacheDict.get(rootDirectory)
    if headerDirs:
        return headerDirs

    output = subprocess.check_output(
        ["find", "-L", rootDirectory, "-name", "*.h"], universal_newlines=True
    )
    headers = output.splitlines()
    headerDirs = set()
    frameworks = set()
    for h in headers:
        frameworkIndex = h.rfind(".framework")
        if frameworkIndex != -1:
            h = os.path.dirname(h[:frameworkIndex])
            frameworks.add(h)
        else:
            h = os.path.dirname(h)
            headerDirs.add(h)
            # contains more one dir for import with module name
            # don't contains more one module name dir. if need, can specify in .flags
            # conflict with #if_include framework check
            #  h = os.path.dirname(h)
            #  headerDirs.add(h)

    headerDirsCacheDict[rootDirectory] = (headerDirs, frameworks)
    return headerDirs, frameworks


def findAllSwiftFiles(rootDirectory):
    output = subprocess.check_output(
        ["find", "-H", rootDirectory, "-name", "*.swift"], universal_newlines=True
    )
    return [os.path.realpath(l) for l in output.splitlines()]


def cmd_split(s):
    import shlex

    return shlex.split(s)  # shlex is more right
    # shlex.split is slow, use a simple version, only consider most case
    # def extract(m):
    #     if m.lastindex == 3:  # \ escape version. remove it
    #         return m.group(m.lastindex).replace("\\ ", " ")
    #     return m.group(m.lastindex)

    # return [extract(m) for m in cmd_split_pattern.finditer(s)]


def readFileArgs(path):
    with open(path) as f:
        return cmd_split(f.read())


# 以文件里的内容作为命令行参数，会进行shell分词展开
def getFileArgs(path, cache) -> List[str]:
    files = cache.get(path)
    if files is None:
        files = readFileArgs(path)
        cache[path] = files
    return files


def filterFlags(items, fileCache):
    """
    f: should return True to accept, return number to skip next number flags
    """
    it = iter(items)
    try:
        while True:
            arg: str = next(it)

            # # -working-directory raise unsupported arg error
            if arg in {
                "-emit-localized-strings-path"
                # "-primary-file", "-o", "-serialize-diagnostics-path", "-working-directory", "-Xfrontend"
            }:
                next(it)
                continue
            # if arg.startswith("-emit"):
            #     if arg.endswith("-path"): next(it)
            #     continue
            if arg in {  # will make sourcekit report errors
                "-use-frontend-parseable-output",
                "-emit-localized-strings",
                # "-frontend", "-c", "-pch-disable-validation", "-index-system-modules", "-enable-objc-interop",
                # '-whole-module-optimization',
            }:
                continue
            if arg == "-filelist":  # sourcekit dont support filelist, unfold it
                yield from getFileArgs(next(it), fileCache)
                continue
            if arg.startswith("@"):
                # swift 5.1 filelist, unfold it
                # xcode 15.3 clang extra args, unfold it
                yield from getFileArgs(arg[1:], fileCache)
                continue
            yield arg
    except StopIteration:
        pass


def findSwiftModuleRoot(filename):
    """return project root or None. if not found"""
    logger.debug("### findSwiftModuleRoot for filename = %s", filename)
    filename = os.path.abspath(filename)
    directory = os.path.dirname(filename)
    flagFile = None
    compileFile = None
    while directory and directory != "/":
        logger.debug("### while loop for dir = %s", directory)
        p = os.path.join(directory, ".swiftflags")
        logger.debug("### p = %s", p)
        if os.path.isfile(p):
            logger.debug("### p is file, returning dir = %s, p = %s, compileFile = %s", directory, p, compileFile)
            return (
                directory,
                p,
                compileFile,
            )  # prefer use swiftflags file as module root directory

        if compileFile is None:
            logger.debug("### compileFile is None, getting compileFile")
            p = os.path.join(directory, ".compile")
            if os.path.isfile(p):
                compileFile = p
                logger.debug("### compileFile changed to %s", compileFile)

        if isProjectRoot(directory):
            logger.debug("### isProjectRoot, breaking")
            break
        else:
            directory = os.path.dirname(directory)
            logger.debug("### !isProjectRoot, dir changed to %s", directory)
    else:
        logger.debug("### returning None, flagFile = %s, compileFile = %s", flagFile, compileFile)
        return (None, flagFile, compileFile)

    logger.debug("### last line, returning dir = %s, flagFile = %s, compileFile = %s", directory, flagFile, compileFile)
    return (directory, flagFile, compileFile)

def filekey(filename):
    return os.path.realpath(filename).lower()

class CompileFileInfo:
    def __init__(self, compileFile, store):
        self.file_info = {}  # {file: command}
        self.dir_info = None  # {dir: set[file key]}
        self.cmd_info = None  # {cmd: set[file key]}

        # load compileFile into info
        import json

        with open(compileFile) as f:
            m: List[dict] = json.load(f)
            for i in m:
                command = i.get("command")
                if not command:
                    continue
                if files := i.get("files"):  # batch files, eg: swift module
                    self.file_info.update((filekey(f), command) for f in files)
                if fileLists := i.get(
                    "fileLists"
                ):  # file list store in a dedicated file
                    self.file_info.update(
                        (filekey(f), command)
                        for l in fileLists
                        if os.path.isfile(l)
                        for f in getFileArgs(l, store.setdefault("filelist", {}))
                    )
                if file := i.get("file"):  # single file info
                    self.file_info[filekey(file)] = command

    def get(self, filename):
        if command := self.file_info.get(filename.lower()):
            # xcode 12 escape =, but not recognized...
            return command.replace("\\=", "=")

    def groupby_dir(self) -> dict[str, set[str]]:
        if self.dir_info is None:  # lazy index dir and cmd
            self.dir_info = defaultdict(set)
            self.cmd_info = defaultdict(set)
            for f, cmd in self.file_info.items():
                self.dir_info[os.path.dirname(f)].add(f)
                self.cmd_info[cmd].add(f)

        return self.dir_info

    # hack new file into current compile file
    # return: set of filekey for match for file. or None if new_file can't be infered
    def new_file(self, filename):
        # Currently only processing swift files
        if not filename.endswith(".swift"):
            return

        filename = os.path.realpath(filename)
        filename_key = filename.lower()
        if filename_key in self.file_info:
            return {filename_key}  # already handled

        dir = os.path.dirname(filename_key)
        samefile = next(
            (v for v in self.groupby_dir().get(dir, ()) if v.endswith(".swift")), None
        )
        if not samefile:
            return

        command = self.file_info[samefile]
        cmd_match = next(cmd_split_pattern.finditer(command), None)
        if not cmd_match:
            return
        assert self.cmd_info # init in groupby_dir
        module_files = self.cmd_info.pop(command)
        index = cmd_match.end()
        from shlex import quote

        command = "".join((command[:index], " ", quote(filename), command[index:]))

        # update command info
        self.groupby_dir()[dir].add(filename_key)
        module_files.add(filename_key)
        self.cmd_info[command] = module_files
        for v in module_files:
            self.file_info[v] = command
        return module_files

def newfileForCompileFile(filename, compileFile, store) -> Optional[set[str]]:
    if not compileFile: return None
    info = compileFileInfoFromStore(compileFile, store)
    return info.new_file(filename)

def commandForFile(filename, compileFile, store: Dict):
    """
    command = store["compile"][<compileFile>][filename]
    > will lazy build compile_file info
    """
    info = compileFileInfoFromStore(compileFile, store)
    return info.get(filename)


def compileFileInfoFromStore(compileFile, store: Dict):
    compile_store = store.setdefault("compile", {})
    info: CompileFileInfo = compile_store.get(compileFile)
    if info is None:  # load {filename.lower: command} dict
        # cache first to avoid re enter when error
        info = CompileFileInfo(compileFile, store)
        compile_store[compileFile] = info

        # if has additional new_file, generate command for it
        for file in store.get("additional_files") or ():
            info.new_file(file)
    return info


def GetFlagsInCompile(filename, compileFile, store):
    """read flags from compileFile. filename should be realpath"""
    if compileFile:
        command = commandForFile(filename, compileFile, store)
        logger.debug("### for file %s, compileFile = %s, command = %s", filename, compileFile, command)
        if command:
            flags = cmd_split(command)[1:]  # ignore executable
            logger.debug("### flags = %s", flags)
            return list(filterFlags(flags, store.setdefault("filelist", {})))


def GetFlags(filename: str, compileFile=None, store=None):
    """sourcekit entry function"""
    # NOTE: use store to ensure toplevel storage. child store should be other name
    # see store.setdefault to get all child attributes
    if store is None:
        logger.debug("### store is None: store = globalStore")
        store = globalStore

    logger.debug("### compileFile: %s", compileFile)
    logger.debug("### filename: %s", filename)
    logger.debug("### store: %s", store)

    if compileFile:
        if final_flags := GetFlagsInCompile(filename, compileFile, store):
            logger.debug("### returning final_flags = %s", final_flags)
            return final_flags

    logger.debug("### returning None")
    return None


# TODO: c family infer flags #
def InferFlagsForSwift(filename, compileFile, store, config):
    """try infer flags by convention and workspace files"""
    project_root, flagFile, compileFile = findSwiftModuleRoot(filename)
    logging.debug(f"infer root: {project_root}, {compileFile}")
    final_flags = GetFlagsInCompile(filename, compileFile, store)

    if not final_flags and flagFile:
        final_flags = []
        headers, frameworks = findAllHeaderDirectory(project_root, store)
        for h in headers:
            final_flags += ["-Xcc", "-I" + h]
        for f in frameworks:
            final_flags.append("-F" + f)
        swiftfiles = findAllSwiftFiles(project_root)
        final_flags += swiftfiles
        a = additionalFlags(flagFile)
        if a:
            # sourcekit not allow same swift name. so if same name, use the find one to support move file
            swift_names = set(os.path.basename(p) for p in swiftfiles)
            final_flags += (
                arg
                for arg in filterFlags(a, store.setdefault("filelist", {}))
                if os.path.basename(arg) not in swift_names
            )
        else:
            final_flags += [
                "-sdk",
                # TODO: Find a way to determined the right platform dynamically (xcrun + project setup reads)
                # "/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk/",
                "/Applications/Xcode.app/Contents/Developer/Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS18.1.sdk",
                "-I/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib/swift",
                "-L/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib/swift/iphonesimulator",
            ]

    if not final_flags:
        final_flags = collectFlagsFromIntermediates(f"{config.build_root}/Build/Intermediates.noindex")
        final_flags += [
            "-sdk",
            # TODO: Find a way to determined the right platform dynamically (xcrun + project setup reads)
            # "/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk/",
            "/Applications/Xcode.app/Contents/Developer/Platforms/iPhoneSimulator.platform/Developer/SDKs/iPhoneSimulator18.1.sdk",
            "-I/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib/swift",
            "-L/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib/swift/iphonesimulator",
            "-L/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib",
            "-L/Applications/Xcode.app/Contents/Developer/Platforms/iPhoneSimulator.platform/Developer/SDKs/iPhoneSimulator.sdk/usr/lib",
            "-target",
            "arm64-apple-ios17.5-simulator",
            filename,
        ]

    if not final_flags:
        final_flags = [
            filename,
            "-sdk",
            # TODO: Find a way to determined the right platform dynamically (xcrun + project setup reads)
            # "/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk/",
            "/Applications/Xcode.app/Contents/Developer/Platforms/iPhoneOS.platform/Developer/SDKs/iPhoneOS18.1.sdk",
            "-I/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib/swift",
            "-L/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib/swift/iphonesimulator",
        ]

    return final_flags

def collectFlagsFromIntermediates(intermediates_path):
    """Collect -I and -F flags from Intermediates.noindex."""
    final_flags = []

    for root, dirs, files in os.walk(intermediates_path):
        # Check if the directory contains .modulemap or .swiftmodule
        for file in files:
            if file.endswith(".modulemap") or file.endswith(".swiftmodule"):
                final_flags.append("-I" + root)
                final_flags.append("-F" + root)
                break  # No need to check further in this directory

    return final_flags

# Copyright 2014-present PlatformIO <contact@platformio.org>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import re
import sys
import shutil
import subprocess
import os
from os.path import isfile, isdir, join

from SCons.Script import (
    ARGUMENTS, COMMAND_LINE_TARGETS, AlwaysBuild, Builder, Default,
    DefaultEnvironment)

from platformio.util import get_serial_ports

env = DefaultEnvironment()
platform = env.PioPlatform()

#
# Helpers
#

FRAMEWORK_DIR = platform.get_package_dir("framework-arduinoespressif32")

IDF_TOOLS_PATH_DEFAULT = os.path.join(os.path.expanduser("~"), ".espressif")
IDF_TOOLS = join(platform.get_package_dir("tl-install"), "tools", "idf_tools.py")
IDF_TOOLS_FLAG = ["install"]
IDF_TOOLS_CMD = [env["PYTHONEXE"], IDF_TOOLS] + IDF_TOOLS_FLAG

# IDF Install is needed only one times
if not os.path.exists(join(IDF_TOOLS_PATH_DEFAULT, "tools")):
    rc = subprocess.call(IDF_TOOLS_CMD)
    if rc != 0:
        sys.stderr.write("Error: Couldn't execute 'idf_tools.py install' \n")
    else:
        print("source dir:", join(IDF_TOOLS_PATH_DEFAULT, "tools"))
        print("target dir:", "".join(env.subst("$PROJECT_PACKAGES_DIR")))
        rc = shutil.copytree(join(IDF_TOOLS_PATH_DEFAULT, "tools"), "".join(env.subst("$PROJECT_PACKAGES_DIR")), symlinks=True, ignore=None, copy_function=copy2, ignore_dangling_symlinks=True, dirs_exist_ok=True)
        print("result copytree:", rc)


def BeforeUpload(target, source, env):
    upload_options = {}
    if "BOARD" in env:
        upload_options = env.BoardConfig().get("upload", {})

    env.AutodetectUploadPort()

    before_ports = get_serial_ports()
    if upload_options.get("use_1200bps_touch", False):
        env.TouchSerialPort("$UPLOAD_PORT", 1200)

    if upload_options.get("wait_for_upload_port", False):
        env.Replace(UPLOAD_PORT=env.WaitForNewSerialPort(before_ports))


def _get_board_memory_type(env):
    board_config = env.BoardConfig()
    default_type = "%s_%s" % (
        board_config.get("build.flash_mode", "dio"),
        board_config.get("build.psram_type", "qspi"),
    )

    return board_config.get(
        "build.memory_type",
        board_config.get(
            "build.%s.memory_type"
            % env.subst("$PIOFRAMEWORK").strip().replace(" ", "_"),
            default_type,
        ),
    )

def _normalize_frequency(frequency):
    frequency = str(frequency).replace("L", "")
    return str(int(int(frequency) / 1000000)) + "m"


def _get_board_f_flash(env):
    frequency = env.subst("$BOARD_F_FLASH")
    return _normalize_frequency(frequency)


def _get_board_f_image(env):
    board_config = env.BoardConfig()
    if "build.f_image" in board_config:
        return _normalize_frequency(board_config.get("build.f_image"))

    return _get_board_f_flash(env)

def _get_board_f_boot(env):
    board_config = env.BoardConfig()
    if "build.f_boot" in board_config:
        return _normalize_frequency(board_config.get("build.f_boot"))

    return _get_board_f_flash(env)


def _get_board_flash_mode(env):
    if _get_board_memory_type(env) in (
        "opi_opi",
        "opi_qspi",
    ):
        return "dout"

    mode = env.subst("$BOARD_FLASH_MODE")
    if mode in ("qio", "qout"):
        return "dio"
    return mode


def _get_board_boot_mode(env):
    memory_type = env.BoardConfig().get("build.arduino.memory_type", "")
    build_boot = env.BoardConfig().get("build.boot", "$BOARD_FLASH_MODE")
    if memory_type in ("opi_opi", "opi_qspi"):
        build_boot = "opi"
    return build_boot


def _parse_size(value):
    if isinstance(value, int):
        return value
    elif value.isdigit():
        return int(value)
    elif value.startswith("0x"):
        return int(value, 16)
    elif value[-1].upper() in ("K", "M"):
        base = 1024 if value[-1].upper() == "K" else 1024 * 1024
        return int(value[:-1]) * base
    return value


def _parse_partitions(env):
    partitions_csv = env.subst("$PARTITIONS_TABLE_CSV")
    if not isfile(partitions_csv):
        sys.stderr.write("Could not find the file %s with partitions "
                         "table.\n" % partitions_csv)
        env.Exit(1)
        return

    result = []
    next_offset = 0
    bound = int(board.get("upload.offset_address", "0x10000"), 16) # default 0x10000
    with open(partitions_csv) as fp:
        for line in fp.readlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            tokens = [t.strip() for t in line.split(",")]
            if len(tokens) < 5:
                continue
            partition = {
                "name": tokens[0],
                "type": tokens[1],
                "subtype": tokens[2],
                "offset": tokens[3] or next_offset,
                "size": tokens[4],
                "flags": tokens[5] if len(tokens) > 5 else None
            }
            result.append(partition)
            next_offset = _parse_size(partition["offset"])
            if (partition["subtype"] == "ota_0"):
                bound = next_offset
            next_offset = (next_offset + bound - 1) & ~(bound - 1)
    # Configure application partition offset
    env.Replace(ESP32_APP_OFFSET=str(hex(bound)))
    # Propagate application offset to debug configurations
    env["INTEGRATION_EXTRA_DATA"].update({"application_offset": str(hex(bound))})
    return result


def _update_max_upload_size(env):
    if not env.get("PARTITIONS_TABLE_CSV"):
        return
    sizes = {
        p["subtype"]: _parse_size(p["size"]) for p in _parse_partitions(env)
        if p["type"] in ("0", "app")
    }

    partitions = {p["name"]: p for p in _parse_partitions(env)}

    # User-specified partition name has the highest priority
    custom_app_partition_name = board.get("build.app_partition_name", "")
    if custom_app_partition_name:
        selected_partition = partitions.get(custom_app_partition_name, {})
        if selected_partition:
            board.update("upload.maximum_size", _parse_size(selected_partition["size"]))
            return
        else:
            print(
                "Warning! Selected partition `%s` is not available in the partition " \
                "table! Default partition will be used!" % custom_app_partition_name
            )

    for p in partitions.values():
        if p["type"] in ("0", "app") and p["subtype"] in ("ota_0"):
            board.update("upload.maximum_size", _parse_size(p["size"]))
            break



def _to_unix_slashes(path):
    return path.replace("\\", "/")


#
# Filesystem helpers
#


def fetch_fs_size(env):
    fs = None
    for p in _parse_partitions(env):
        if p["type"] == "data" and p["subtype"] in ("spiffs", "fat", "littlefs"):
            fs = p
    if not fs:
        sys.stderr.write(
            "Could not find the any filesystem section in the partitions "
            "table %s\n" % env.subst("$PARTITIONS_TABLE_CSV")
        )
        env.Exit(1)
        return
    env["FS_START"] = _parse_size(fs["offset"])
    env["FS_SIZE"] = _parse_size(fs["size"])
    env["FS_PAGE"] = int("0x100", 16)
    env["FS_BLOCK"] = int("0x1000", 16)

    # FFat specific offsets, see:
    # https://github.com/lorol/arduino-esp32fatfs-plugin#notes-for-fatfs
    if filesystem == "fatfs":
        env["FS_START"] += 4096
        env["FS_SIZE"] -= 4096


def __fetch_fs_size(target, source, env):
    fetch_fs_size(env)
    return (target, source)


board = env.BoardConfig()
mcu = board.get("build.mcu", "esp32")
toolchain_arch = "xtensa-%s" % mcu
filesystem = board.get("build.filesystem", "spiffs")
if mcu in ("esp32c2", "esp32c3", "esp32c6", "esp32h2"):
    toolchain_arch = "riscv32-esp"

if "INTEGRATION_EXTRA_DATA" not in env:
    env["INTEGRATION_EXTRA_DATA"] = {}

env.Replace(
    __get_board_boot_mode=_get_board_boot_mode,
    __get_board_f_flash=_get_board_f_flash,
    __get_board_f_image=_get_board_f_image,
    __get_board_f_boot=_get_board_f_boot,
    __get_board_flash_mode=_get_board_flash_mode,
    __get_board_memory_type=_get_board_memory_type,

    AR="%s-elf-gcc-ar" % toolchain_arch,
    AS="%s-elf-as" % toolchain_arch,
    CC="%s-elf-gcc" % toolchain_arch,
    CXX="%s-elf-g++" % toolchain_arch,
    GDB=join(
        platform.get_package_dir(
            "tl-rv-gdb"
            if mcu in ("esp32c2", "esp32c3", "esp32c6")
            else "tl-xt-gdb"
        )
        or "",
        "bin",
        "%s-elf-gdb" % toolchain_arch,
    ),
    OBJCOPY=join(platform.get_package_dir("tl-esptool") or "", "esptool.py"),
    RANLIB="%s-elf-gcc-ranlib" % toolchain_arch,
    SIZETOOL="%s-elf-size" % toolchain_arch,

    ARFLAGS=["rc"],

    SIZEPROGREGEXP=r"^(?:\.iram0\.text|\.iram0\.vectors|\.dram0\.data|\.flash\.text|\.flash\.rodata|)\s+([0-9]+).*",
    SIZEDATAREGEXP=r"^(?:\.dram0\.data|\.dram0\.bss|\.noinit)\s+([0-9]+).*",
    SIZECHECKCMD="$SIZETOOL -A -d $SOURCES",
    SIZEPRINTCMD="$SIZETOOL -B -d $SOURCES",

    ERASEFLAGS=[
        "--chip", mcu,
        "--port", '"$UPLOAD_PORT"'
    ],
    ERASECMD='"$PYTHONEXE" "$OBJCOPY" $ERASEFLAGS erase_flash',

    # mkspiffs package contains two different binaries for IDF and Arduino
    MKFSTOOL="mk%s" % filesystem
    + (
        (
            "_${PIOPLATFORM}_"
            + (
                "espidf"
                if "espidf" in env.subst("$PIOFRAMEWORK")
                else "${PIOFRAMEWORK}"
            )
        )
        if filesystem == "spiffs"
        else ""
    ),
    # Legacy `ESP32_SPIFFS_IMAGE_NAME` is used as the second fallback value for
    # backward compatibility
    ESP32_FS_IMAGE_NAME=env.get(
        "ESP32_FS_IMAGE_NAME", env.get("ESP32_SPIFFS_IMAGE_NAME", filesystem)
    ),

    ESP32_APP_OFFSET=env.get("INTEGRATION_EXTRA_DATA").get("application_offset"),

    PROGSUFFIX=".elf"
)

# Allow user to override via pre:script
if env.get("PROGNAME", "program") == "program":
    env.Replace(PROGNAME="firmware")

env.Append(
    BUILDERS=dict(
        ElfToBin=Builder(
            action=env.VerboseAction(" ".join([
                '"$PYTHONEXE" "$OBJCOPY"',
                "--chip", mcu, "elf2image",
                "--flash_mode", "${__get_board_flash_mode(__env__)}",
                "--flash_freq", "${__get_board_f_image(__env__)}",
                "--flash_size", board.get("upload.flash_size", "4MB"),
                "-o", "$TARGET", "$SOURCES"
            ]), "Building $TARGET"),
            suffix=".bin"
        ),
        DataToBin=Builder(
            action=env.VerboseAction(
                " ".join(
                    ['"$MKFSTOOL"', "-c", "$SOURCES", "-s", "$FS_SIZE"]
                    + (
                        [
                            "-p",
                            "$FS_PAGE",
                            "-b",
                            "$FS_BLOCK",
                        ]
                        if filesystem in ("littlefs", "spiffs")
                        else []
                    )
                    + ["$TARGET"]
                ),
                "Building FS image from '$SOURCES' directory to $TARGET",
            ),
            emitter=__fetch_fs_size,
            source_factory=env.Dir,
            suffix=".bin",
        ),
    )
)

if not env.get("PIOFRAMEWORK"):
    env.SConscript("frameworks/_bare.py", exports="env")

#
# Target: Build executable and linkable firmware or FS image
#

target_elf = None
if "nobuild" in COMMAND_LINE_TARGETS:
    target_elf = join("$BUILD_DIR", "${PROGNAME}.elf")
    if set(["uploadfs", "uploadfsota"]) & set(COMMAND_LINE_TARGETS):
        fetch_fs_size(env)
        target_firm = join("$BUILD_DIR", "${ESP32_FS_IMAGE_NAME}.bin")
    else:
        target_firm = join("$BUILD_DIR", "${PROGNAME}.bin")
else:
    target_elf = env.BuildProgram()
    if set(["buildfs", "uploadfs", "uploadfsota"]) & set(COMMAND_LINE_TARGETS):
        target_firm = env.DataToBin(
            join("$BUILD_DIR", "${ESP32_FS_IMAGE_NAME}"), "$PROJECT_DATA_DIR"
        )
        env.NoCache(target_firm)
        AlwaysBuild(target_firm)
    else:
        target_firm = env.ElfToBin(
            join("$BUILD_DIR", "${PROGNAME}"), target_elf)
        env.Depends(target_firm, "checkprogsize")

env.AddPlatformTarget("buildfs", target_firm, target_firm, "Build Filesystem Image")
AlwaysBuild(env.Alias("nobuild", target_firm))
target_buildprog = env.Alias("buildprog", target_firm, target_firm)

# update max upload size based on CSV file
if env.get("PIOMAINPROG"):
    env.AddPreAction(
        "checkprogsize",
        env.VerboseAction(
            lambda source, target, env: _update_max_upload_size(env),
            "Retrieving maximum program size $SOURCES"))

#
# Target: Print binary size
#

target_size = env.AddPlatformTarget(
    "size",
    target_elf,
    env.VerboseAction("$SIZEPRINTCMD", "Calculating size $SOURCE"),
    "Program Size",
    "Calculate program size",
)

#
# Target: Upload firmware or FS image
#

upload_protocol = env.subst("$UPLOAD_PROTOCOL")
debug_tools = board.get("debug.tools", {})
upload_actions = []

# Compatibility with old OTA configurations
if (upload_protocol != "espota"
        and re.match(r"\"?((([0-9]{1,3}\.){3}[0-9]{1,3})|[^\\/]+\.local)\"?$",
                     env.get("UPLOAD_PORT", ""))):
    upload_protocol = "espota"
    sys.stderr.write(
        "Warning! We have just detected `upload_port` as IP address or host "
        "name of ESP device. `upload_protocol` is switched to `espota`.\n"
        "Please specify `upload_protocol = espota` in `platformio.ini` "
        "project configuration file.\n")

if upload_protocol == "espota":
    if not env.subst("$UPLOAD_PORT"):
        sys.stderr.write(
            "Error: Please specify IP address or host name of ESP device "
            "using `upload_port` for build environment or use "
            "global `--upload-port` option.\n"
            "See https://docs.platformio.org/page/platforms/"
            "espressif32.html#over-the-air-ota-update\n")
    env.Replace(
        UPLOADER=join(FRAMEWORK_DIR,"tools", "espota.py"),
        UPLOADERFLAGS=["--debug", "--progress", "-i", "$UPLOAD_PORT"],
        UPLOADCMD='"$PYTHONEXE" "$UPLOADER" $UPLOADERFLAGS -f $SOURCE'
    )
    if set(["uploadfs", "uploadfsota"]) & set(COMMAND_LINE_TARGETS):
        env.Append(UPLOADERFLAGS=["--spiffs"])
    upload_actions = [env.VerboseAction("$UPLOADCMD", "Uploading $SOURCE")]

elif upload_protocol == "esptool":
    env.Replace(
        UPLOADER=join(
            platform.get_package_dir("tl-esptool") or "", "esptool.py"),
        UPLOADERFLAGS=[
            "--chip", mcu,
            "--port", '"$UPLOAD_PORT"',
            "--baud", "$UPLOAD_SPEED",
            "--before", board.get("upload.before_reset", "default_reset"),
            "--after", board.get("upload.after_reset", "hard_reset"),
            "write_flash", "-z",
            "--flash_mode", "${__get_board_flash_mode(__env__)}",
            "--flash_freq", "${__get_board_f_image(__env__)}",
            "--flash_size", "detect"
        ],
        UPLOADCMD='"$PYTHONEXE" "$UPLOADER" $UPLOADERFLAGS $ESP32_APP_OFFSET $SOURCE'
    )
    for image in env.get("FLASH_EXTRA_IMAGES", []):
        env.Append(UPLOADERFLAGS=[image[0], env.subst(image[1])])

    if "uploadfs" in COMMAND_LINE_TARGETS:
        env.Replace(
            UPLOADERFLAGS=[
                "--chip", mcu,
                "--port", '"$UPLOAD_PORT"',
                "--baud", "$UPLOAD_SPEED",
                "--before", board.get("upload.before_reset", "default_reset"),
                "--after", board.get("upload.after_reset", "hard_reset"),
                "write_flash", "-z",
                "--flash_mode", "${__get_board_flash_mode(__env__)}",
                "--flash_freq", "${__get_board_f_image(__env__)}",
                "--flash_size", "detect",
                "$FS_START"
            ],
            UPLOADCMD='"$PYTHONEXE" "$UPLOADER" $UPLOADERFLAGS $SOURCE',
        )

    upload_actions = [
        env.VerboseAction(BeforeUpload, "Looking for upload port..."),
        env.VerboseAction("$UPLOADCMD", "Uploading $SOURCE")
    ]

elif upload_protocol == "dfu":

    hwids = board.get("build.hwids", [["0x2341", "0x0070"]])
    vid = hwids[0][0]
    pid = hwids[0][1]

    upload_actions = [env.VerboseAction("$UPLOADCMD", "Uploading $SOURCE")]

    env.Replace(
        UPLOADER=join(
            platform.get_package_dir("tl-dfuutil") or "", "dfu-util"
        ),
        UPLOADERFLAGS=[
            "-d",
            ",".join(["%s:%s" % (hwid[0], hwid[1]) for hwid in hwids]),
            "-Q",
            "-D"
        ],
        UPLOADCMD='"$UPLOADER" $UPLOADERFLAGS "$SOURCE"',
    )


elif upload_protocol in debug_tools:
    _parse_partitions(env)
    openocd_args = ["-d%d" % (2 if int(ARGUMENTS.get("PIOVERBOSE", 0)) else 1)]
    openocd_args.extend(
        debug_tools.get(upload_protocol).get("server").get("arguments", []))
    openocd_args.extend(
        [
            "-c",
            "adapter speed %s" % env.GetProjectOption("debug_speed", "5000"),
            "-c",
            "program_esp {{$SOURCE}} %s verify"
            % (
                "$FS_START"
                if "uploadfs" in COMMAND_LINE_TARGETS
                else env.get("INTEGRATION_EXTRA_DATA").get("application_offset")
            ),
        ]
    )
    if "uploadfs" not in COMMAND_LINE_TARGETS:
        for image in env.get("FLASH_EXTRA_IMAGES", []):
            openocd_args.extend(
                [
                    "-c",
                    "program_esp {{%s}} %s verify"
                    % (_to_unix_slashes(image[1]), image[0]),
                ]
            )
    openocd_args.extend(["-c", "reset run; shutdown"])
    openocd_args = [
        f.replace(
            "$PACKAGE_DIR",
            _to_unix_slashes(
                platform.get_package_dir("tl-openocd") or ""))
        for f in openocd_args
    ]
    env.Replace(
        UPLOADER="openocd",
        UPLOADERFLAGS=openocd_args,
        UPLOADCMD="$UPLOADER $UPLOADERFLAGS",
    )
    upload_actions = [env.VerboseAction("$UPLOADCMD", "Uploading $SOURCE")]

# custom upload tool
elif upload_protocol == "custom":
    upload_actions = [env.VerboseAction("$UPLOADCMD", "Uploading $SOURCE")]

else:
    sys.stderr.write("Warning! Unknown upload protocol %s\n" % upload_protocol)

env.AddPlatformTarget("upload", target_firm, upload_actions, "Upload")
env.AddPlatformTarget("uploadfs", target_firm, upload_actions, "Upload Filesystem Image")
env.AddPlatformTarget(
    "uploadfsota", target_firm, upload_actions, "Upload Filesystem Image OTA")

#
# Target: Erase Flash and Upload
#

env.AddPlatformTarget(
    "erase_upload",
    target_firm,
    [
        env.VerboseAction(BeforeUpload, "Looking for upload port..."),
        env.VerboseAction("$ERASECMD", "Erasing..."),
        env.VerboseAction("$UPLOADCMD", "Uploading $SOURCE")
    ],
    "Erase Flash and Upload",
)

#
# Target: Erase Flash
#

env.AddPlatformTarget(
    "erase",
    None,
    [
        env.VerboseAction(BeforeUpload, "Looking for upload port..."),
        env.VerboseAction("$ERASECMD", "Erasing...")
    ],
    "Erase Flash",
)

#
# Override memory inspection behavior
#

env.SConscript("sizedata.py", exports="env")

#
# Default targets
#

Default([target_buildprog, target_size])

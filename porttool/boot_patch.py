from sys import stderr
import subprocess
from os import unlink, chmod
from os import name as osname
from os.path import isfile, isdir
import logging
from hashlib import sha1
from shutil import copyfile, rmtree
import zipfile

from .archdetect import retTypeAndMachine


def getsha1(filename):
    with open(filename, 'rb') as f:
        return sha1(f.read()).hexdigest()


def cp(src, dest):
    if isfile(src):
        copyfile(src, dest)


def rm(*files):
    for i in files:
        if isdir(i):
            rmtree(i)
        else:
            if isfile(i):
                unlink(i)


def grep_prop(key, file) -> str:
    with open(file, 'r') as f:
        for i in iter(f.readline, ""):
            if key in i:
                return i.split("=")[1].rstrip("\n")


def parseMagiskApk(apk: str, arch: ["arm64", "arm", "x86", "x86_64"] = "arm64", log=stderr):
    """
    This function will extract useful file from magisk.apk
    """

    def archconv(a):
        ret = a
        match a:
            case "arm64":
                ret = "arm64-v8a"
            case "arm":
                ret = "armeabi-v7a"
        return ret

    def archto32(a):
        ret = a
        match a:
            case "arm64-v8a":
                ret = "armeabi-v7a"
            case "x86_64":
                ret = "x86"
        return ret

    def saveto(bytes, path):
        with open(path, 'wb') as f:
            f.write(bytes)

    print("- Start unzipping the required files...", file=log)
    arch = archconv(arch)
    os, p = retTypeAndMachine()
    pp = "x86_64"
    if p == "aarch64":
        pp = "arm64-v8a"
    elif p == "arm":
        pp = "armeabi-v7a"
    with zipfile.ZipFile(apk) as z:
        for l in z.filelist:
            # 26.0+
            if "stub.apk" in l.filename:
                saveto(z.read(l), "stub.apk")
            # Save a platform magiskboot into bin if linux
            if os != 'win' and osname != 'nt':
                if f"lib/{pp}/libmagiskboot.so" in l.filename:
                    saveto(z.read(l), "bin/magiskboot")
                    chmod("bin/magiskboot", 0o755)

            if f"lib/{arch}/libmagiskinit.so" in l.filename:
                saveto(z.read(f"lib/{archto32(arch)}/libmagisk32.so"), "magisk32")
                if arch in ["arm64-v8a", "x86_64"]:
                    saveto(z.read(f"lib/{arch}/libmagisk64.so"), "magisk64")
                saveto(z.read(f"lib/{arch}/libmagiskinit.so"), "magiskinit")


class BootPatcher(object):
    def __init__(
            self,
            magiskboot,
            keep_verity: bool = True,
            keep_forceencrypt: bool = True,
            patchvbmeta_flag: bool = False,
            recovery_mode: bool = False,
            legacysar: bool = False,
            progress=None,
            log=stderr,
    ):
        self.magiskboot = magiskboot

        self.keep_verity = keep_verity
        self.keep_forceencrypt = keep_forceencrypt
        self.patchvbmeta_flag = patchvbmeta_flag
        self.recovery_mode = recovery_mode
        self.legacysar = legacysar
        self.progress = progress

        self.log = log

        self.__check()
        self.__prepare_env()

    def __check(self):
        if not isfile(self.magiskboot):
            print("- The magiskboot file does not exist and initialization cannot be completed", file=self.log)
            return False

    def __prepare_env(self):
        bool2str = lambda x: "true" if x else "flase"
        self.env = {
            "KEEPVERITY": bool2str(self.keep_verity),
            "KEEPFORCEENCRYPT": bool2str(self.keep_forceencrypt),
            "PATCHVBMETAFLAG": bool2str(self.patchvbmeta_flag),
            "RECOVERYMODE": bool2str(self.recovery_mode),
            "LEGACYSAR": bool2str(self.legacysar),
        }

        # This maybe no need
        # for i in self.env:
        #    putenv(i, self.env[i])

    def __execv(self, cmd: list):
        """
        Run magiskboot command, already include magiskboot
        return returncode and output
        """
        full = [
            self.magiskboot,
            *cmd
        ]

        if osname == 'nt':
            creationflags = subprocess.CREATE_NO_WINDOW
        else:
            creationflags = 0
        logging.info("Run command : \n" + " ".join(full))
        ret = subprocess.run(full,
                             stderr=subprocess.STDOUT,
                             stdout=subprocess.PIPE,
                             shell=False,
                             env=self.env,
                             creationflags=creationflags,
                             )
        logging.info(ret.stdout.decode())
        return ret.returncode, ret.stdout.decode()

    def patch(self, bootimg: str) -> bool:
        # Check bootimg exist
        if not isfile(bootimg):
            print("- The boot image does not exist", file=self.log)
            return False

        # Unpack bootimg
        print("- Unpack the boot image...", file=self.log)
        err, ret = self.__execv(["unpack", bootimg])
        logging.info(ret)

        match err:
            case 0:
                pass
            case 1:
                print("! Unsupported/Unknown image format", file=self.log)
                return False
            case 2:
                print("- ChromeOS format boot image", file=self.log)
                print("- Not supported yet")
                return False
            case _:
                print("! Unable to unpack the boot image", file=self.log)
                return False

        print("- Check ramdisk status", file=self.log)
        if isfile("ramdisk.cpio"):
            err, ret = self.__execv(["cpio", "ramdisk.cpio", "test"])
            status = err
            skip_backup = ""
        else:
            status = 0
            skip_backup = "#"

        sha = ""
        match (status & 3):
            case 0:  # Stock boot
                print("- Original unmodified boot image detected", file=self.log)
                sha = getsha1(bootimg)
                cp(bootimg, "stock_boot.img")
                cp("ramdisk.cpio", "ramdisk.cpio.orig")
            case 1:  # Magisk patched
                print("- Detected a boot image patched by magisk", file=self.log)
                err, ret = self.__execv(["cpio", "ramdisk.cpio", "extract .backup/.magisk config.orig", "restore"])
                cp("ramdisk.cpio", "ramdisk.cpio.orig")
                rm("stock_boot.img")
            case 2:  # Unsupported
                print("- The boot image has been modified by an unknown program", file=self.log)
                print("- Please restore it to the original boot image first", file=self.log)
                return False

        # Sony device
        init = "init"
        if not (status & 4) == 0:
            init = "init.real"

        if isfile("config.orig"):
            sha = grep_prop("SHA1", "config.orig")
            rm("config.orig")

        print("- Patching ramdisk", file=self.log)

        skip32 = "#"
        skip64 = "#"

        if isfile("magisk64"):
            self.__execv(["compress=xz", "magisk64", "magisk64.xz"])
            skip64 = ""
        if isfile("magisk32"):
            self.__execv(["compress=xz", "magisk32", "magisk32.xz"])
            skip32 = ""

        stub = False
        if isfile("stub.apk"):
            stub = True

        if stub:
            self.__execv(["compress=xz", "stub.apk", "stub.xz"])
        with open("config", 'w') as config:
            config.write(
                f"KEEPVERITY={self.env['KEEPVERITY']}" + "\n" +
                f"KEEPFORCEENCRYPT={self.env['KEEPFORCEENCRYPT']}" + "\n" +
                f"RECOVERYMODE={self.env['RECOVERYMODE']}" + "\n")
            if sha != "":
                config.write(f"SHA1={sha}\n")

        err, _ = self.__execv([
            "cpio", "ramdisk.cpio",
            f"add 0750 {init} magiskinit",
            "mkdir 0750 overlay.d",
            "mkdir 0750 overlay.d/sbin",
            f"{skip32} add 0644 overlay.d/sbin/magisk32.xz magisk32.xz",
            f"{skip64} add 0644 overlay.d/sbin/magisk64.xz magisk64.xz",
            "add 0644 overlay.d/sbin/stub.xz stub.xz" if stub else "",
            "patch",
            f"{skip_backup} backup ramdisk.cpio.orig",
            "mkdir 000 .backup",
            "add 000 .backup/.magisk config",
        ])
        if err != 0:
            print("- Unable to patch ramdisk", file=self.log)
            return False

        rm("ramdisk.cpio.orig", "config", "magisk32.xz", "magisk64.xz", "stub.xz")

        for dt in "dtb", "kernel_dtb", "extra":
            if isfile(dt):
                err, _ = self.__execv([
                    "dtb", dt, "test"
                ])
                if err != 0:
                    print(f"- Boot 镜像中的{dt}被旧的magisk修补过", file=self.log)
                    print("- Please try again with an unmodified boot image.", file=self.log)
                    return False
                err, _ = self.__execv([
                    "dtb", dt, "patch"
                ])
                if err == 0:
                    print(f"- Patch fstab of {dt} in boot image")

        if isfile("kernel"):
            patchedkernel = False
            err, _ = self.__execv([
                "hexpatch", "kernel",
                "49010054011440B93FA00F71E9000054010840B93FA00F7189000054001840B91FA00F7188010054",
                "A1020054011440B93FA00F7140020054010840B93FA00F71E0010054001840B91FA00F7181010054"
            ])
            if err == 0: patchedkernel = True
            err, _ = self.__execv([
                "hexpatch", "kernel", "821B8012", "E2FF8F12"
            ])
            if err == 0: patchedkernel = True
            if self.legacysar:
                err, _ = self.__execv([
                    "hexpatch", "kernel",
                    "736B69705F696E697472616D667300",
                    "77616E745F696E697472616D667300"
                ])
                if err == 0: patchedkernel = True
            if not patchedkernel: rm("kernel")

        print("- Package boot image", file=self.log)
        err, _ = self.__execv([
            "repack", bootimg
        ])
        if err != 0:
            print("- Failed to pack the boot image", file=self.log)
            return False

        self.cleanup()
        print("- Finish!", file=self.log)
        return True

    def cleanup(self):
        rmlist = [
            "magisk32", "magisk32.xz", "magisk64", "magisk64.xz", "magiskinit", "stub.apk"
        ]
        rm(*rmlist)
        print("- clean files", file=self.log)
        self.__execv(["cleanup"])


if __name__ == "__main__":
    print(grep_prop("B", "config"))

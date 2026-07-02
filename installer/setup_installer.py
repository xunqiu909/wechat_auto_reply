import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path


APP_NAME = 'WxAutoAutoReply'
EXE_NAME = 'wxauto_auto_reply.exe'
GUIDE_NAME = 'user_guide.md'
SHORTCUT_NAME = 'WxAuto Auto Reply.lnk'


def resource_path(name):
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    return base / name


def message(text, title='WxAuto Auto Reply Setup', flags=0x40):
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, flags)
    except Exception:
        print(text)


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def create_shortcut(target, workdir, shortcut):
    command = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut({shortcut});"
        "$s.TargetPath={target};"
        "$s.WorkingDirectory={workdir};"
        "$s.IconLocation={target};"
        "$s.Save()"
    ).format(
        shortcut=ps_quote(shortcut),
        target=ps_quote(target),
        workdir=ps_quote(workdir),
    )
    subprocess.run(
        ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command],
        check=False,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )


def main():
    try:
        install_dir = Path(os.environ.get('LOCALAPPDATA', Path.home())) / APP_NAME
        desktop = Path(os.path.join(os.environ.get('USERPROFILE', str(Path.home())), 'Desktop'))
        shortcut = desktop / SHORTCUT_NAME

        install_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resource_path(EXE_NAME), install_dir / EXE_NAME)
        shutil.copy2(resource_path(GUIDE_NAME), install_dir / GUIDE_NAME)

        uninstall = install_dir / 'uninstall.cmd'
        uninstall.write_text(
            '@echo off\n'
            'del "%USERPROFILE%\\Desktop\\WxAuto Auto Reply.lnk" 2>nul\n'
            'rmdir /s /q "%LOCALAPPDATA%\\WxAutoAutoReply"\n'
            'echo Uninstalled WxAuto Auto Reply.\n'
            'pause\n',
            encoding='ascii',
        )

        create_shortcut(install_dir / EXE_NAME, install_dir, shortcut)
        message(
            '安装完成。\n\n'
            '安装目录: {}\n'
            '桌面快捷方式: {}\n\n'
            '首次使用前请先登录微信 PC 客户端。'.format(install_dir, shortcut)
        )
        return 0
    except Exception as exc:
        message('安装失败:\n{}'.format(exc), flags=0x10)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

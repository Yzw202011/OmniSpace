/* OmniSpace 启动器（P8 观感升级：bat → exe）
 *
 * 大白话：这就是一颗「双击的按钮」——找到自己所在的包目录，拉起包内
 * 自带的 pythonw 跑 boot.py，全程零黑窗。编译：见 tools/build_launcher_exe.py
 */
#include <windows.h>
#include <stdio.h>
#include <string.h>

int WINAPI WinMain(HINSTANCE hInst, HINSTANCE hPrev,
                   LPSTR lpCmdLine, int nShow)
{
    (void)hInst; (void)hPrev; (void)nShow;
    char root[MAX_PATH], py[MAX_PATH * 2], args[MAX_PATH * 4];
    GetModuleFileNameA(NULL, root, MAX_PATH);
    char *slash = strrchr(root, '\\');
    if (slash)
        *slash = '\0';

    snprintf(py, sizeof(py), "%s\\runtime\\py310\\pythonw.exe", root);
    snprintf(args, sizeof(args), "\"%s\\launcher\\boot.py\"", root);
    if (lpCmdLine && lpCmdLine[0]) {
        strncat(args, " ", sizeof(args) - strlen(args) - 1);
        strncat(args, lpCmdLine, sizeof(args) - strlen(args) - 1);
    }

    if (GetFileAttributesA(py) == INVALID_FILE_ATTRIBUTES) {
        MessageBoxA(NULL,
                    "Missing runtime\\py310\\pythonw.exe - package incomplete",
                    "OmniSpace", MB_ICONERROR);
        return 1;
    }
    SetCurrentDirectoryA(root);
    HINSTANCE r = ShellExecuteA(NULL, "open", py, args, root, SW_HIDE);
    if ((INT_PTR)r <= 32) {
        MessageBoxA(NULL, "Failed to launch pythonw.exe", "OmniSpace",
                    MB_ICONERROR);
        return 1;
    }
    return 0;
}

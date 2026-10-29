@echo off
REM vcvars64.bat relies on reg.exe, which is on the sandbox program blacklist here,
REM so it cannot resolve the SDK path. Build INCLUDE/LIB/PATH by hand instead.
set MSVC=C:\BuildTools\VC\Tools\MSVC\14.44.35207
set SDKINC=C:\Program Files (x86)\Windows Kits\10\Include\10.0.22621.0
set SDKLIB=C:\Program Files (x86)\Windows Kits\10\Lib\10.0.22621.0

set PATH=%MSVC%\bin\Hostx64\x64;%PATH%
set INCLUDE=%MSVC%\include;%SDKINC%\ucrt;%SDKINC%\shared;%SDKINC%\um;%SDKINC%\winrt
set LIB=%MSVC%\lib\x64;%SDKLIB%\ucrt\x64;%SDKLIB%\um\x64

cl /nologo /EHsc /W3 hello.cpp /Fe:hello_msvc.exe
if errorlevel 1 (
  echo COMPILE_FAILED
  exit /b 1
)
echo BUILD_OK

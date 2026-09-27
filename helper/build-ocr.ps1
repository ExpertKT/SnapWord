# Build the standalone OCR helper (ocr-helper.exe).
#
# Sources live in helper\src\ (vendored copies of the author's SnapWheel OCR layer);
# override with -SrcDir if you want to build against a live SnapWheel checkout instead.
# No NuGet, no Visual Studio: the .NET Framework compiler that ships with Windows is enough.
param([string]$SrcDir = '')
$ErrorActionPreference = 'Stop'

$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $SrcDir) { $SrcDir = Join-Path $here 'src' }
$netDir = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
$csc    = Join-Path $netDir 'csc.exe'
$winrt  = Join-Path $netDir 'System.Runtime.WindowsRuntime.dll'
$out    = Join-Path $here 'ocr-helper.exe'

if (-not (Test-Path $csc)) { Write-Error "csc.exe not found: $csc"; exit 1 }

$sources = @(
    (Join-Path $srcDir '56-Ocr.cs'),
    (Join-Path $srcDir '57-OcrNative.cs'),
    (Join-Path $srcDir '92-OcrTall.cs'),
    (Join-Path $here   'Stubs.cs'),
    (Join-Path $here   'Program.cs')
)
foreach ($s in $sources) {
    if (-not (Test-Path $s)) { Write-Error "missing source: $s"; exit 1 }
}

$refs = @('/r:System.dll', '/r:System.Core.dll', '/r:System.Drawing.dll')
if (Test-Path $winrt) { $refs += "/r:$winrt" }
else { Write-Warning "System.Runtime.WindowsRuntime.dll missing - the system OCR engine will not compile in" }

$cscArgs = @('/nologo', '/noconfig', '/target:exe', '/platform:anycpu', '/codepage:65001', "/out:$out") + $refs + $sources
& $csc @cscArgs
$code = $LASTEXITCODE
if ($code -ne 0) { Write-Host "csc failed (exit $code)" -ForegroundColor Red; exit $code }
Write-Host "built: $out"

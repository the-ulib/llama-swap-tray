# Used only by the optional coordinator installer. Ordinary installs need no Python.
function Get-CoordinatorPython([string]$Root, [string]$Python) {
    $venvPython = Join-Path $Root '.coord-env\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython) { return $venvPython }
    $candidate = $Python
    if (-not $candidate) {
        $found = Get-Command python -ErrorAction SilentlyContinue
        if ($found -and $found.Source -notmatch '\\Microsoft\\WindowsApps\\') { $candidate = $found.Source }
    }
    if ($candidate) {
        & $candidate -c 'import sys,venv; sys.exit(0 if sys.version_info >= (3,10) and sys.maxsize > 2**32 else 1)'
        if ($LASTEXITCODE -ne 0) { throw 'The optional coordinator requires a 64-bit Python 3.10+ with venv support' }
    } else {
        $pythonDir = Join-Path $Root 'runtime\python'
        $candidate = Join-Path $pythonDir 'python.exe'
        if (-not (Test-Path -LiteralPath $candidate)) {
            $installer = Join-Path $Root 'runtime\python-installer.exe'
            # Official, Authenticode-verified runtime, isolated from ComfyUI.
            Invoke-WebRequest 'https://www.python.org/ftp/python/3.14.9/python-3.14.9-amd64.exe' -OutFile $installer
            $signature = Get-AuthenticodeSignature -LiteralPath $installer
            if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
                throw 'Python installer signature could not be verified'
            }
            $arguments = '/quiet InstallAllUsers=0 TargetDir="{0}" Include_launcher=0 InstallLauncherAllUsers=0 PrependPath=0 AppendPath=0 Shortcuts=0 Include_test=0 Include_doc=0 Include_tcltk=0 AssociateFiles=0 Include_pip=1' -f $pythonDir
            $process = Start-Process -FilePath $installer -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
            if ($process.ExitCode -notin @(0, 3010) -or -not (Test-Path -LiteralPath $candidate)) { throw 'Python runtime installation failed' }
        }
    }
    & $candidate -m venv (Join-Path $Root '.coord-env')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the isolated coordinator environment; use -Python with a standard Python installation' }
    return $venvPython
}

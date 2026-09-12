# Trusted Sandbox controller. The request is data, never interpolated source.
param([Parameter(Mandatory=$true)][string]$RequestPath)
$ErrorActionPreference = 'Stop'
$request = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
Add-Type -TypeDefinition @'
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading.Tasks;
public static class AgentlyShellCapture {
    public sealed class Result {
        public int returncode;
        public bool timed_out;
        public bool stdout_truncated;
        public bool stderr_truncated;
    }
    private sealed class Capture { public volatile bool Truncated; }
    private static async Task Drain(Stream source, string path, int limit, Capture capture) {
        int kept = 0;
        using (var output = new FileStream(path, FileMode.Create, FileAccess.Write, FileShare.Read)) {
            byte[] buffer = new byte[8192];
            int size;
            while ((size = await source.ReadAsync(buffer, 0, buffer.Length)) != 0) {
                int count = Math.Min(size, limit - kept);
                if (count > 0) {
                    await output.WriteAsync(buffer, 0, count);
                    output.Flush();
                    kept += count;
                }
                if (count < size) capture.Truncated = true;
            }
        }
    }
    public static Result Run(string binary, string arguments, string cwd,
                             string directory, int limit, int milliseconds,
                             System.Collections.IDictionary environment) {
        var info = new ProcessStartInfo(binary, arguments);
        info.WorkingDirectory = cwd;
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.RedirectStandardInput = true;
        info.RedirectStandardOutput = true;
        info.RedirectStandardError = true;
        // These are guest system values, not inherited Host credentials.
        info.EnvironmentVariables.Clear();
        foreach (string key in new string[] { "PATH", "SystemRoot", "WINDIR", "TEMP", "TMP" }) {
            string value = Environment.GetEnvironmentVariable(key);
            if (value != null) info.EnvironmentVariables[key] = value;
        }
        foreach (System.Collections.DictionaryEntry item in environment)
            info.EnvironmentVariables[(string)item.Key] = (string)item.Value;
        var clock = Stopwatch.StartNew();
        using (var process = Process.Start(info)) {
            process.StandardInput.Close();
            var outState = new Capture();
            var errState = new Capture();
            var stdout = Drain(process.StandardOutput.BaseStream, Path.Combine(directory, "stdout"), limit, outState);
            var stderr = Drain(process.StandardError.BaseStream, Path.Combine(directory, "stderr"), limit, errState);
            bool exited = process.WaitForExit(milliseconds);
            int remaining = (int)Math.Max(0L, milliseconds - clock.ElapsedMilliseconds);
            bool drained = exited && Task.WaitAll(new Task[] { stdout, stderr }, remaining);
            if (!drained) {
                // Host stops the entire VM before accepting any output. Never
                // wait indefinitely for descendants retaining inherited pipes.
                if (!process.HasExited) process.Kill();
                return new Result { returncode = 124, timed_out = true,
                                    stdout_truncated = outState.Truncated, stderr_truncated = errState.Truncated };
            }
            return new Result { returncode = process.ExitCode, timed_out = false,
                                stdout_truncated = outState.Truncated, stderr_truncated = errState.Truncated };
        }
    }
}
'@
$environment = @{}
foreach ($entry in $request.env.PSObject.Properties) { $environment[$entry.Name] = [string]$entry.Value }
$result = [AgentlyShellCapture]::Run(
    [string]$request.binary, [string]$request.arguments, [string]$request.cwd,
    [string]$request.output, [int]$request.limit, [int]$request.milliseconds, $environment)
$json = $result | ConvertTo-Json -Compress
[IO.File]::WriteAllText((Join-Path $request.output 'result.json'), $json, (New-Object Text.UTF8Encoding($false)))

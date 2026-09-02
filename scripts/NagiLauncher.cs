using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class NagiLauncher
{
    private const string Title = "Nagi";

    [STAThread]
    private static int Main(string[] arguments)
    {
        try
        {
            bool stop = false;
            bool noOpen = false;
            int port = 8765;
            foreach (string argument in arguments)
            {
                if (string.Equals(argument, "stop", StringComparison.OrdinalIgnoreCase))
                    stop = true;
                else if (string.Equals(argument, "--no-open", StringComparison.Ordinal))
                    noOpen = true;
                else if (argument.StartsWith("--port=", StringComparison.Ordinal)
                    && int.TryParse(argument.Substring(7), out port)
                    && port >= 1 && port <= 65535)
                {
                }
                else
                    throw new InvalidOperationException("不支持的启动参数。");
            }

            string root = AppDomain.CurrentDomain.BaseDirectory;
            string python = Path.Combine(root, "runtime", "pythonw.exe");
            if (!File.Exists(python))
                throw new FileNotFoundException(
                    "便携运行环境不完整。请重新完整解压 Nagi 便携版后再启动。",
                    python
                );

            string action = stop ? "stop" : "start";
            string pythonArguments = "-I -m nagi.launcher " + action + " --port " + port;
            if (noOpen)
                pythonArguments += " --no-open";

            ProcessStartInfo start = new ProcessStartInfo
            {
                FileName = python,
                Arguments = pythonArguments,
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden
            };
            start.EnvironmentVariables["NAGI_APP_ROOT"] = root;
            start.EnvironmentVariables["PYTHONUTF8"] = "1";
            using (Process process = Process.Start(start))
            {
                process.WaitForExit();
                return process.ExitCode;
            }
        }
        catch (Exception error)
        {
            MessageBox.Show(
                error.Message,
                Title + " 启动失败",
                MessageBoxButtons.OK,
                MessageBoxIcon.Error
            );
            return 1;
        }
    }
}

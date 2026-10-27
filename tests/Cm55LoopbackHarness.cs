// Test-only transport substitution. Never packaged with the production bridge.
// Connects exclusively to 127.0.0.2; invokes the actual serial-profile operations.
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

internal static class Cm55LoopbackHarness
{
    static Type bridge;
    static void Set(string key, object value) { bridge.GetField(key, BindingFlags.NonPublic | BindingFlags.Static).SetValue(null, value); }
    static object Call(Type type, object instance, string name, params object[] args) {
        return type.GetMethod(name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static | BindingFlags.Instance).Invoke(instance, args);
    }
    [STAThread]
    static int Main(string[] args)
    {
        Console.OutputEncoding = new UTF8Encoding(false);
        var json = new JavaScriptSerializer { MaxJsonLength = 8000000 };
        Socket socket = null;
        try {
            bool firmware = args[3] == "firmware";
            var vendor = Assembly.LoadFrom(Path.GetFullPath(args[1]));
            bridge = Assembly.LoadFrom(Path.GetFullPath(args[0])).GetType(firmware ? "ReuseFirmwareUpgrade" : "ReuseDeviceBridge", true);
            string prefix = firmware ? "Downloadconfig." : "BonengDrivesoft.";
            Type ft = vendor.GetType(prefix + (firmware ? "Form1" : "form.Form2"), true);
            object form = Call(ft, null, firmware ? "getinstance" : "getInstance");
            Type ct = vendor.GetType(prefix + "communication.CCommModbus", true);
            object comm = Call(ct, null, "getInstance");
            socket = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
            socket.Connect(new IPEndPoint(IPAddress.Parse("127.0.0.2"), 1000));
            ft.GetField("comm_method").SetValue(form, 2);
            ft.GetField("newclient").SetValue(form, socket);
            ft.GetField("Control_comm").SetValue(form, 1);
            Set("vendor", vendor); Set("commType", ct); Set("comm", comm);
            Set("entityType", vendor.GetType(prefix+"communication.EntityModbus", true));
            Set("actionType", vendor.GetType(prefix+"communication.IAction", true));
            Set(firmware ? "network" : "socket", socket);
            var input = json.Deserialize<Dictionary<string, object>>(File.ReadAllText(args[2]));
            object result;
            if (firmware) result = Call(bridge, null, "Cm55Fingerprint", input);
            else {
                Set("watchdog", new Timer(delegate {}, null, Timeout.Infinite, Timeout.Infinite));
                var identity = (Dictionary<string, object>)Call(bridge, null, "BoundIdentity", false, input);
                result = Call(bridge, null, "Operate", input, identity, false);
                FieldInfo queueField = bridge.GetField("logQueue", BindingFlags.NonPublic | BindingFlags.Static);
                var queue = (BlockingCollection<object>)queueField.GetValue(null);
                if (queue != null) { queue.CompleteAdding(); ((Thread)bridge.GetField("logThread", BindingFlags.NonPublic | BindingFlags.Static).GetValue(null)).Join(3000); }
            }
            Console.WriteLine(json.Serialize(new { harness_status = "passed", result = result })); return 0;
        } catch (Exception error) {
            while (error is TargetInvocationException && error.InnerException != null) error = error.InnerException;
            Console.WriteLine(json.Serialize(new { harness_status = "failed", error = error.Message })); return 1;
        } finally { if (socket != null) socket.Dispose(); }
    }
}

// Inspect .NET Framework assembly metadata and IL without executing its code.
using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Reflection.Emit;
using System.Runtime.InteropServices;
using System.Text;

internal static class InspectReuse
{
    private static string directory;
    private static readonly Dictionary<short, OpCode> opcodes = new Dictionary<short, OpCode>();

    private static Assembly Resolve(object sender, ResolveEventArgs args)
    {
        AssemblyName name = new AssemblyName(args.Name);
        string local = Path.Combine(directory, name.Name + ".dll");
        if (File.Exists(local)) return Assembly.ReflectionOnlyLoadFrom(local);
        string framework = Path.Combine(RuntimeEnvironment.GetRuntimeDirectory(), name.Name + ".dll");
        if (File.Exists(framework)) return Assembly.ReflectionOnlyLoadFrom(framework);
        return Assembly.ReflectionOnlyLoad(args.Name);
    }

    private static string Quoted(string value)
    {
        return "\"" + value.Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\r", "\\r").Replace("\n", "\\n") + "\"";
    }

    private static string Token(Module module, int token, bool isString)
    {
        try
        {
            if (isString) return Quoted(module.ResolveString(token));
            MemberInfo member = module.ResolveMember(token);
            return (member.DeclaringType == null ? "" : member.DeclaringType.FullName + "::") + member;
        }
        catch { return "token:0x" + token.ToString("X8"); }
    }

    private static void Body(MethodBase method)
    {
        Console.WriteLine("METHOD " + method.DeclaringType.FullName + "::" + method + " public=" + method.IsPublic + " static=" + method.IsStatic);
        MethodBody body = method.GetMethodBody();
        if (body == null) return;
        foreach (LocalVariableInfo variable in body.LocalVariables) Console.WriteLine("  LOCAL " + variable);
        byte[] il = body.GetILAsByteArray();
        int position = 0;
        while (position < il.Length)
        {
            int start = position;
            short number = il[position++];
            if (number == 0xFE) number = (short)(0xFE00 | il[position++]);
            OpCode opcode = opcodes[number];
            string operand = "";
            switch (opcode.OperandType)
            {
                case OperandType.ShortInlineI: operand = ((sbyte)il[position++]).ToString(); break;
                case OperandType.InlineI: operand = BitConverter.ToInt32(il, position).ToString(); position += 4; break;
                case OperandType.InlineI8: operand = BitConverter.ToInt64(il, position).ToString(); position += 8; break;
                case OperandType.ShortInlineR: operand = BitConverter.ToSingle(il, position).ToString(); position += 4; break;
                case OperandType.InlineR: operand = BitConverter.ToDouble(il, position).ToString(); position += 8; break;
                case OperandType.ShortInlineVar: operand = il[position++].ToString(); break;
                case OperandType.InlineVar: operand = BitConverter.ToUInt16(il, position).ToString(); position += 2; break;
                case OperandType.ShortInlineBrTarget:
                    int shortOffset = (sbyte)il[position++]; operand = "IL_" + (position + shortOffset).ToString("X4"); break;
                case OperandType.InlineBrTarget:
                    int offset = BitConverter.ToInt32(il, position); position += 4; operand = "IL_" + (position + offset).ToString("X4"); break;
                case OperandType.InlineSwitch:
                    int count = BitConverter.ToInt32(il, position); position += 4;
                    int end = position + 4 * count;
                    for (int i = 0; i < count; i++) { operand += " IL_" + (end + BitConverter.ToInt32(il, position)).ToString("X4"); position += 4; }
                    break;
                case OperandType.InlineString:
                    operand = Token(method.Module, BitConverter.ToInt32(il, position), true); position += 4; break;
                case OperandType.InlineField:
                case OperandType.InlineMethod:
                case OperandType.InlineType:
                case OperandType.InlineTok:
                case OperandType.InlineSig:
                    operand = Token(method.Module, BitConverter.ToInt32(il, position), false); position += 4; break;
            }
            Console.WriteLine("  IL_" + start.ToString("X4") + " " + opcode.Name + " " + operand);
        }
        foreach (ExceptionHandlingClause handler in body.ExceptionHandlingClauses)
            Console.WriteLine("  HANDLER " + handler.Flags + " try=" + handler.TryOffset + ":" + handler.TryLength + " handler=" + handler.HandlerOffset + ":" + handler.HandlerLength);
    }

    private static int Main(string[] args)
    {
        try
        {
            Console.OutputEncoding = new UTF8Encoding(false);
            if (args.Length < 2) throw new ArgumentException("Usage: InspectReuse assembly-path type-filter [more-type-filters]");
            directory = Path.GetDirectoryName(Path.GetFullPath(args[0]));
            AppDomain.CurrentDomain.ReflectionOnlyAssemblyResolve += Resolve;
            foreach (FieldInfo field in typeof(OpCodes).GetFields(BindingFlags.Public | BindingFlags.Static))
                if (field.FieldType == typeof(OpCode)) { OpCode value = (OpCode)field.GetValue(null); opcodes[value.Value] = value; }
            Assembly assembly = Assembly.ReflectionOnlyLoadFrom(Path.GetFullPath(args[0]));
            Console.WriteLine("ASSEMBLY " + assembly.FullName);
            if (args[1] == "--list")
            {
                foreach (Type type in assembly.GetTypes())
                {
                    Console.WriteLine("TYPE " + type.FullName);
                    foreach (MethodInfo method in type.GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly))
                        Console.WriteLine("  METHOD " + method);
                }
                return 0;
            }
            BindingFlags flags = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly;
            foreach (Type type in assembly.GetTypes())
            {
                bool selected = false;
                for (int i = 1; i < args.Length; i++) if (type.FullName.IndexOf(args[i].Split(new string[] { "::" }, StringSplitOptions.None)[0], StringComparison.OrdinalIgnoreCase) >= 0) selected = true;
                if (!selected) continue;
                Console.WriteLine("TYPE " + type.FullName);
                foreach (FieldInfo field in type.GetFields(flags)) Console.WriteLine("FIELD " + field.FieldType + " " + field.Name + " static=" + field.IsStatic + " public=" + field.IsPublic);
                foreach (ConstructorInfo constructor in type.GetConstructors(flags)) if (Selected(type, constructor, args)) Body(constructor);
                foreach (MethodInfo method in type.GetMethods(flags)) if (Selected(type, method, args)) Body(method);
            }
            return 0;
        }
        catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }

    private static bool Selected(Type type, MethodBase method, string[] args)
    {
        for (int i = 1; i < args.Length; i++)
        {
            string[] filter = args[i].Split(new string[] { "::" }, StringSplitOptions.None);
            if (type.FullName.IndexOf(filter[0], StringComparison.OrdinalIgnoreCase) < 0) continue;
            if (filter.Length == 1 || method.Name == filter[1]) return true;
        }
        return false;
    }
}

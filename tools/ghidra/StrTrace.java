// Decompiles functions referencing strings that contain any of the given substrings.
// Args: <outfile> <substring> [substring...]
// @category LMU
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Data;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.Reference;

import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.util.*;

public class StrTrace extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] a = getScriptArgs();
        DecompInterface dec = new DecompInterface();
        dec.openProgram(currentProgram);
        Map<Function, Set<String>> hits = new LinkedHashMap<>();
        try (PrintWriter w = new PrintWriter(a[0], StandardCharsets.UTF_8)) {
            w.println("# StrTrace: " + currentProgram.getName());
            for (int i = 1; i < a.length; i++) {
                String needle = a[i];
                byte[] pat = needle.getBytes(StandardCharsets.US_ASCII);
                w.println("\n## \"" + needle + "\"");
                Address start = currentProgram.getMinAddress();
                int n = 0;
                while (start != null && n < 20) {
                    Address hit = currentProgram.getMemory().findBytes(start, currentProgram.getMaxAddress(), pat, null, true, monitor);
                    if (hit == null) break;
                    n++;
                    Set<Address> targets = new LinkedHashSet<>();
                    targets.add(hit);
                    Data d = getDataContaining(hit);
                    if (d != null) targets.add(d.getAddress());
                    for (Address t : targets) {
                        for (Reference r : getReferencesTo(t)) {
                            Function f = getFunctionContaining(r.getFromAddress());
                            w.println("  " + t + " <- " + r.getFromAddress() + (f != null ? " in " + f.getName() + " @ " + f.getEntryPoint() : ""));
                            if (f != null) hits.computeIfAbsent(f, k -> new LinkedHashSet<>()).add(needle);
                        }
                    }
                    start = hit.add(1);
                }
                if (n == 0) w.println("  not found");
            }
            for (Map.Entry<Function, Set<String>> e : hits.entrySet()) {
                Function f = e.getKey();
                w.println("\n//==== " + f.getName() + " @ " + f.getEntryPoint() + "  refs " + e.getValue() + " ====");
                w.print("// callers:");
                for (Function c : f.getCallingFunctions(monitor)) w.print(" " + c.getName());
                w.println();
                DecompileResults r = dec.decompileFunction(f, 240, monitor);
                if (r != null && r.decompileCompleted()) {
                    String c = r.getDecompiledFunction().getC();
                    if (c.length() > 40000) c = c.substring(0, 40000) + "\n/* truncated */";
                    w.println(c);
                } else {
                    w.println("/* decompile failed */");
                }
            }
        }
        dec.dispose();
        println("Wrote " + a[0]);
    }
}

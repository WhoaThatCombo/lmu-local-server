// Decompiles the given functions and their callees down to a depth, skipping huge/library ones.
// Args: <outfile> <depth> <addr> [addr...]
// @category LMU
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Function;

import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.util.*;

public class DecompTree extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] a = getScriptArgs();
        String out = a[0];
        int depth = Integer.parseInt(a[1]);
        DecompInterface dec = new DecompInterface();
        dec.openProgram(currentProgram);
        Set<Function> done = new LinkedHashSet<>();
        Deque<Object[]> q = new ArrayDeque<>();
        for (int i = 2; i < a.length; i++) {
            Function f = getFunctionAt(toAddr(Long.parseUnsignedLong(a[i].replace("0x", ""), 16)));
            if (f == null) f = getFunctionContaining(toAddr(Long.parseUnsignedLong(a[i].replace("0x", ""), 16)));
            if (f != null) q.add(new Object[]{f, 0, "root"});
        }
        try (PrintWriter w = new PrintWriter(out, StandardCharsets.UTF_8)) {
            int budget = 120;
            while (!q.isEmpty() && budget > 0) {
                Object[] e = q.poll();
                Function f = (Function) e[0];
                int d = (Integer) e[1];
                if (!done.add(f) || f.isThunk() || f.isExternal()) continue;
                if (f.getBody().getNumAddresses() > 60000) {
                    w.println("\n//==== " + f.getName() + " skipped (huge) from " + e[2]);
                    continue;
                }
                budget--;
                w.println("\n//==== " + f.getName() + " @ " + f.getEntryPoint() + " depth " + d + " from " + e[2] + " ====");
                DecompileResults r = dec.decompileFunction(f, 240, monitor);
                if (r != null && r.decompileCompleted()) {
                    String c = r.getDecompiledFunction().getC();
                    if (c.length() > 400000) c = c.substring(0, 400000) + "\n/* truncated */";
                    w.println(c);
                } else {
                    w.println("/* decompile failed */");
                }
                if (d < depth) {
                    for (Function c : f.getCalledFunctions(monitor)) q.add(new Object[]{c, d + 1, f.getName()});
                }
            }
        }
        dec.dispose();
        println("Wrote " + out);
    }
}

// Reads N pointers at an address (a vtable) and decompiles each target, following callees to a depth.
// Args: <outfile> <addr> <count> <depth>
// @category LMU
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.util.*;

public class VtDecomp extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] a = getScriptArgs();
        Address vt = toAddr(Long.parseUnsignedLong(a[1].replace("0x", ""), 16));
        int count = Integer.parseInt(a[2]);
        int depth = Integer.parseInt(a[3]);
        DecompInterface dec = new DecompInterface();
        dec.openProgram(currentProgram);
        Set<Function> done = new HashSet<>();
        try (PrintWriter w = new PrintWriter(a[0], StandardCharsets.UTF_8)) {
            Deque<Object[]> q = new ArrayDeque<>();
            for (int i = 0; i < count; i++) {
                long p = getLong(vt.add(8L * i));
                Address t = toAddr(p);
                Function f = getFunctionAt(t);
                w.println("// vt[" + i + "] = " + t + " " + (f == null ? "?" : f.getName()));
                if (f != null) q.add(new Object[]{f, 0, "vt[" + i + "]"});
            }
            int budget = 40;
            while (!q.isEmpty() && budget-- > 0) {
                Object[] e = q.poll();
                Function f = (Function) e[0];
                int d = (Integer) e[1];
                if (!done.add(f) || f.isThunk()) continue;
                if (f.getBody().getNumAddresses() > 40000) { w.println("\n//==== " + f.getName() + " huge"); continue; }
                w.println("\n//==== " + f.getName() + " @ " + f.getEntryPoint() + " depth " + d + " from " + e[2]);
                DecompileResults r = dec.decompileFunction(f, 120, monitor);
                w.println(r != null && r.decompileCompleted() ? r.getDecompiledFunction().getC() : "/* failed */");
                if (d < depth) for (Function c : f.getCalledFunctions(monitor)) q.add(new Object[]{c, d + 1, f.getName()});
            }
        }
        dec.dispose();
    }
}

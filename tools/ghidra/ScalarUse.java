// Functions containing an instruction with the given scalar operand; prints decompiled lines mentioning it.
// Args: <outfile> <hexscalar> [maxFuncs]
// @category LMU
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.*;
import ghidra.program.model.scalar.Scalar;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.util.*;

public class ScalarUse extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] a = getScriptArgs();
        long val = Long.parseLong(a[1].replace("0x", ""), 16);
        int max = a.length > 2 ? Integer.parseInt(a[2]) : 30;
        Set<Function> funcs = new LinkedHashSet<>();
        InstructionIterator it = currentProgram.getListing().getInstructions(true);
        while (it.hasNext() && !monitor.isCancelled()) {
            Instruction ins = it.next();
            for (int i = 0; i < ins.getNumOperands(); i++) {
                for (Object o : ins.getOpObjects(i)) {
                    if (o instanceof Scalar && ((Scalar) o).getUnsignedValue() == val) {
                        Function f = getFunctionContaining(ins.getAddress());
                        if (f != null) funcs.add(f);
                    }
                }
            }
        }
        DecompInterface dec = new DecompInterface();
        dec.openProgram(currentProgram);
        String hex = "0x" + Long.toHexString(val);
        try (PrintWriter w = new PrintWriter(a[0], StandardCharsets.UTF_8)) {
            w.println("// " + funcs.size() + " functions");
            int n = 0;
            for (Function f : funcs) {
                w.println("\n//==== " + f.getName() + " @ " + f.getEntryPoint());
                if (n++ >= max) continue;
                DecompileResults r = dec.decompileFunction(f, 90, monitor);
                if (r == null || !r.decompileCompleted()) continue;
                String[] lines = r.getDecompiledFunction().getC().split("\n");
                for (int i = 0; i < lines.length; i++)
                    if (lines[i].contains(hex)) {
                        for (int j = Math.max(0, i - 3); j <= Math.min(lines.length - 1, i + 3); j++) w.println(j + ": " + lines[j]);
                        w.println("--");
                    }
            }
        }
        dec.dispose();
    }
}

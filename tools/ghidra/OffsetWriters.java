// Lists instructions that store to [reg + OFFSET] and decompiles their functions, printing lines that mention the offset.
// Args: <outfile> <hexoffset>
// @category LMU
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.*;
import ghidra.program.model.scalar.Scalar;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.util.*;

public class OffsetWriters extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] a = getScriptArgs();
        long off = Long.parseLong(a[1].replace("0x", ""), 16);
        String hex = "0x" + Long.toHexString(off);
        Map<Function, List<String>> hits = new LinkedHashMap<>();
        InstructionIterator it = currentProgram.getListing().getInstructions(true);
        while (it.hasNext() && !monitor.isCancelled()) {
            Instruction ins = it.next();
            String m = ins.getMnemonicString();
            if (!(m.startsWith("MOV") || m.startsWith("INC") || m.startsWith("DEC") || m.startsWith("ADD") || m.startsWith("AND") || m.startsWith("OR"))) continue;
            if (ins.getNumOperands() < 1) continue;
            String op0 = ins.getDefaultOperandRepresentation(0);
            if (!op0.contains("[") || !op0.contains(hex)) continue;
            Function f = getFunctionContaining(ins.getAddress());
            hits.computeIfAbsent(f, k -> new ArrayList<>()).add(ins.getAddress() + "  " + ins);
        }
        DecompInterface dec = new DecompInterface();
        dec.openProgram(currentProgram);
        try (PrintWriter w = new PrintWriter(a[0], StandardCharsets.UTF_8)) {
            for (Map.Entry<Function, List<String>> e : hits.entrySet()) {
                Function f = e.getKey();
                w.println("\n//==== " + (f == null ? "?" : f.getName() + " @ " + f.getEntryPoint()));
                for (String s : e.getValue()) w.println("//   " + s);
                if (f == null) continue;
                DecompileResults r = dec.decompileFunction(f, 120, monitor);
                if (r == null || !r.decompileCompleted()) continue;
                String[] lines = r.getDecompiledFunction().getC().split("\n");
                for (int i = 0; i < lines.length; i++) {
                    if (lines[i].contains(hex)) {
                        for (int j = Math.max(0, i - 6); j <= Math.min(lines.length - 1, i + 2); j++) w.println(j + ": " + lines[j]);
                        w.println("--");
                    }
                }
            }
        }
        dec.dispose();
    }
}

// For each call to the target function, print preceding instructions. Args: <outfile> <before> <targetAddr>
// @category LMU
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.symbol.Reference;

import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;

public class CallSites extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] a = getScriptArgs();
        int before = Integer.parseInt(a[1]);
        try (PrintWriter w = new PrintWriter(a[0], StandardCharsets.UTF_8)) {
            for (int t = 2; t < a.length; t++) {
                var target = toAddr(Long.parseUnsignedLong(a[t].replace("0x", ""), 16));
                for (Reference r : getReferencesTo(target)) {
                    if (!r.getReferenceType().isCall()) continue;
                    Function f = getFunctionContaining(r.getFromAddress());
                    w.println("\n== call " + target + " at " + r.getFromAddress() + " in " + (f != null ? f.getName() : "?"));
                    Instruction ins = getInstructionAt(r.getFromAddress());
                    for (int k = 0; k < before && ins != null && ins.getPrevious() != null; k++) ins = ins.getPrevious();
                    for (int k = 0; k <= before && ins != null; k++) {
                        w.println("   " + ins.getAddress() + "  " + ins);
                        ins = ins.getNext();
                    }
                }
            }
        }
        println("Wrote " + a[0]);
    }
}

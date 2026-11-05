// Prints instructions before/after given addresses. Args: <outfile> <before> <after> <addr...>
// @category LMU
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;

import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;

public class Disasm extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] a = getScriptArgs();
        int before = Integer.parseInt(a[1]), after = Integer.parseInt(a[2]);
        try (PrintWriter w = new PrintWriter(a[0], StandardCharsets.UTF_8)) {
            for (int i = 3; i < a.length; i++) {
                Address at = toAddr(Long.parseUnsignedLong(a[i].replace("0x", ""), 16));
                Function f = getFunctionContaining(at);
                w.println("\n== " + at + " in " + (f != null ? f.getName() : "?"));
                Instruction ins = getInstructionContaining(at);
                if (ins == null) ins = getInstructionAfter(at);
                for (int k = 0; k < before && ins != null; k++) {
                    Instruction p = ins.getPrevious();
                    if (p == null) break;
                    ins = p;
                }
                for (int k = 0; k < before + after && ins != null; k++) {
                    w.println((ins.getAddress().equals(at) ? "=> " : "   ") + ins.getAddress() + "  " + ins);
                    ins = ins.getNext();
                }
            }
        }
        println("Wrote " + a[0]);
    }
}

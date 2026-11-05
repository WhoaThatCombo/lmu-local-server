// Lists all references (read/write) to given data addresses with instruction text. Args: <out> <addr...>
// @category LMU
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.symbol.Reference;

import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;

public class DataRefs extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] a = getScriptArgs();
        try (PrintWriter w = new PrintWriter(a[0], StandardCharsets.UTF_8)) {
            for (int i = 1; i < a.length; i++) {
                Address at = toAddr(Long.parseUnsignedLong(a[i].replace("0x", ""), 16));
                w.println("== " + at);
                for (Reference r : getReferencesTo(at)) {
                    Function f = getFunctionContaining(r.getFromAddress());
                    Instruction ins = getInstructionContaining(r.getFromAddress());
                    w.println("  " + r.getReferenceType() + " " + r.getFromAddress() + " "
                              + (f != null ? f.getName() : "?") + "  " + ins);
                }
            }
        }
    }
}

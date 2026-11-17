// Export a machine-readable summary of Ghidra's auto-analysis for the recomp pipeline.
// Usage (headless): -postScript ExportAnalysis.java <out.json>
//@category AoE2Recomp
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.*;
import ghidra.program.model.listing.*;
import ghidra.program.model.mem.*;
import ghidra.program.model.symbol.*;
import ghidra.program.model.lang.OperandType;

import java.io.*;
import java.util.*;

public class ExportAnalysis extends GhidraScript {

    private static String hex(Address a) { return "\"0x" + a.toString().replaceFirst("^.*:", "") + "\""; }
    private static String q(String s) {
        StringBuilder b = new StringBuilder("\"");
        for (char c : s.toCharArray()) {
            if (c == '"' || c == '\\') b.append('\\').append(c);
            else if (c < 0x20) b.append(String.format("\\u%04x", (int) c));
            else b.append(c);
        }
        return b.append('"').toString();
    }

    private boolean isExec(Address a) {
        MemoryBlock b = currentProgram.getMemory().getBlock(a);
        return b != null && b.isExecute();
    }

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        String out = args.length > 0 ? args[0] : "analysis.json";
        Program p = currentProgram;
        Listing listing = p.getListing();
        FunctionManager fm = p.getFunctionManager();
        ReferenceManager rm = p.getReferenceManager();
        Memory mem = p.getMemory();

        try (PrintWriter w = new PrintWriter(new BufferedWriter(new FileWriter(out)))) {
            w.println("{");
            w.println("\"program\": " + q(p.getName()) + ",");
            w.println("\"image_base\": " + hex(p.getImageBase()) + ",");

            // ---- memory blocks
            w.println("\"blocks\": [");
            boolean first = true;
            for (MemoryBlock b : mem.getBlocks()) {
                if (!first) w.println(",");
                first = false;
                w.print("  {\"name\": " + q(b.getName()) + ", \"start\": " + hex(b.getStart()) + ", \"end\": " + hex(b.getEnd())
                        + ", \"exec\": " + b.isExecute() + ", \"write\": " + b.isWrite() + ", \"init\": " + b.isInitialized() + "}");
            }
            w.println("\n],");

            // ---- functions
            w.println("\"functions\": [");
            first = true;
            for (Function f : fm.getFunctions(true)) {
                monitor.checkCancelled();
                if (!first) w.println(",");
                first = false;
                AddressSetView body = f.getBody();
                StringBuilder ranges = new StringBuilder();
                for (AddressRange r : body) {
                    if (ranges.length() > 0) ranges.append(",");
                    ranges.append("[").append(hex(r.getMinAddress())).append(",").append(hex(r.getMaxAddress())).append("]");
                }
                int ninsn = 0, fpu = 0, mmx = 0, sse = 0, rets = 0, fsSeh = 0, rep = 0;
                Set<String> calls = new TreeSet<>();
                List<String> icalls = new ArrayList<>();
                List<String> ijmps = new ArrayList<>();
                Map<String, Integer> mnem = new TreeMap<>();
                InstructionIterator it = listing.getInstructions(body, true);
                while (it.hasNext()) {
                    Instruction in = it.next();
                    ninsn++;
                    String m = in.getMnemonicString().toUpperCase();
                    String txt = in.toString().toUpperCase();
                    mnem.merge(m, 1, Integer::sum);
                    if (m.startsWith("F") && !m.equals("FS")) fpu++;
                    if (txt.contains("XMM")) sse++;
                    else if (txt.matches(".*\\bMM[0-7]\\b.*") || m.equals("EMMS")) mmx++;
                    if (m.startsWith("RET")) rets++;
                    if (txt.contains("FS:[0X0]") || txt.contains("FS:[0]")) fsSeh++;
                    if (m.startsWith("REP")) rep++;
                    boolean isCall = in.getFlowType().isCall();
                    boolean isJump = in.getFlowType().isJump();
                    if (isCall || isJump) {
                        int ot = in.getNumOperands() > 0 ? in.getOperandType(0) : 0;
                        boolean indirect = in.getFlowType().isComputed() || (ot & OperandType.REGISTER) != 0
                                || (ot & OperandType.INDIRECT) != 0 || (ot & OperandType.DYNAMIC) != 0;
                        if (isCall && !indirect) {
                            for (Address t : in.getFlows()) calls.add(hex(t));
                        } else if (indirect) {
                            StringBuilder tg = new StringBuilder();
                            for (Reference r : rm.getReferencesFrom(in.getAddress())) {
                                if (r.getReferenceType().isFlow() || r.getReferenceType().isData()) {
                                    if (tg.length() > 0) tg.append(",");
                                    tg.append(hex(r.getToAddress()));
                                }
                            }
                            String op = in.getDefaultOperandRepresentation(0);
                            String rec = "{\"at\": " + hex(in.getAddress()) + ", \"op\": " + q(op) + ", \"refs\": [" + tg + "]}";
                            if (isCall) icalls.add(rec); else ijmps.add(rec);
                        }
                    }
                }
                Function thunked = f.isThunk() ? f.getThunkedFunction(false) : null;
                int callers = f.getCallingFunctions(monitor).size();
                w.print("  {\"entry\": " + hex(f.getEntryPoint()) + ", \"name\": " + q(f.getName())
                        + ", \"thunk\": " + f.isThunk()
                        + (thunked != null ? ", \"thunk_to\": " + q(thunked.getName(true)) : "")
                        + ", \"external\": " + f.isExternal()
                        + ", \"size\": " + body.getNumAddresses()
                        + ", \"ranges\": [" + ranges + "]"
                        + ", \"ninsn\": " + ninsn + ", \"fpu\": " + fpu + ", \"mmx\": " + mmx + ", \"sse\": " + sse
                        + ", \"rets\": " + rets + ", \"seh\": " + fsSeh + ", \"rep\": " + rep
                        + ", \"no_return\": " + f.hasNoReturn()
                        + ", \"cc\": " + q(f.getCallingConventionName())
                        + ", \"stack_purge\": " + f.getStackPurgeSize()
                        + ", \"callers\": " + callers
                        + ", \"calls\": [" + String.join(",", calls) + "]"
                        + ", \"icalls\": [" + String.join(",", icalls) + "]"
                        + ", \"ijmps\": [" + String.join(",", ijmps) + "]"
                        + "}");
            }
            w.println("\n],");

            // ---- defined data inside executable blocks (jump tables, constants, strings in code)
            w.println("\"data_in_code\": [");
            first = true;
            DataIterator di = listing.getDefinedData(true);
            while (di.hasNext()) {
                Data d = di.next();
                if (!isExec(d.getAddress())) continue;
                if (!first) w.println(",");
                first = false;
                w.print("  {\"at\": " + hex(d.getAddress()) + ", \"len\": " + d.getLength() + ", \"type\": " + q(d.getDataType().getName()) + "}");
            }
            w.println("\n],");

            // ---- bytes in executable blocks that are neither instructions nor data
            w.println("\"undefined_exec\": [");
            first = true;
            for (MemoryBlock b : mem.getBlocks()) {
                if (!b.isExecute()) continue;
                AddressSet s = new AddressSet(b.getStart(), b.getEnd());
                Address a = b.getStart();
                long runStart = -1; Address rs = null; long len = 0;
                CodeUnitIterator cui = listing.getCodeUnits(s, true);
                while (cui.hasNext()) {
                    CodeUnit cu = cui.next();
                    boolean undef = (cu instanceof Data) && !((Data) cu).isDefined();
                    if (undef) {
                        if (rs == null) { rs = cu.getAddress(); len = 0; }
                        len += cu.getLength();
                    } else if (rs != null) {
                        if (!first) w.println(",");
                        first = false;
                        w.print("  [" + hex(rs) + ", " + len + "]");
                        rs = null;
                    }
                }
                if (rs != null) {
                    if (!first) w.println(",");
                    first = false;
                    w.print("  [" + hex(rs) + ", " + len + "]");
                }
            }
            w.println("\n],");

            // ---- writes into executable memory (self-modifying-code candidates)
            w.println("\"writes_to_exec\": [");
            first = true;
            for (MemoryBlock b : mem.getBlocks()) {
                if (!b.isExecute()) continue;
                AddressIterator ai = rm.getReferenceDestinationIterator(new AddressSet(b.getStart(), b.getEnd()), true);
                while (ai.hasNext()) {
                    Address to = ai.next();
                    for (Reference r : rm.getReferencesTo(to)) {
                        if (!r.getReferenceType().isWrite()) continue;
                        if (!first) w.println(",");
                        first = false;
                        w.print("  {\"from\": " + hex(r.getFromAddress()) + ", \"to\": " + hex(to) + "}");
                    }
                }
            }
            w.println("\n],");

            // ---- pointers to functions stored in data (vtables, callback tables)
            w.println("\"code_ptrs_in_data\": " + countCodePointersInData() );
            w.println("}");
        }
        println("wrote " + out);
    }

    private long countCodePointersInData() throws Exception {
        long n = 0;
        Memory mem = currentProgram.getMemory();
        FunctionManager fm = currentProgram.getFunctionManager();
        for (MemoryBlock b : mem.getBlocks()) {
            if (b.isExecute() || !b.isInitialized()) continue;
            Address a = b.getStart();
            while (a.compareTo(b.getEnd().subtract(3)) <= 0) {
                int v = mem.getInt(a);
                Address t = currentProgram.getAddressFactory().getDefaultAddressSpace().getAddress(v & 0xffffffffL);
                if (fm.getFunctionAt(t) != null) n++;
                a = a.add(4);
            }
        }
        return n;
    }
}

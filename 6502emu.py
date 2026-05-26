# 6502 cpu emulator made by fsminecrafter
# Expanded with full instruction set, UART/serial output, and NASM loader

import os
import sys
import re
import threading
import queue
import time

DEBUG = False

# UART / Serial output device
# Mapped to $6000 (write) and $6001 (status, always ready)
# Write any byte to $6000 -> printed as ASCII to stdout

UART_DATA_ADDR    = 0x6000
UART_STATUS_ADDR  = 0x6001
UART_INPUT_ADDR   = 0x6002

class UART:
    """
    UART:
      $6000 write -> stdout
      $6001 read   -> status
          bit 0 = output ready (always 1)
          bit 1 = input available
      $6002 read   -> next keyboard byte
    Host commands:
      &exit    -> stop emulator
      &pause   -> pause execution
      &unpause -> resume execution
    """

    def __init__(self):
        self._input_q = queue.Queue()
        self._cpu = None
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._input_loop, daemon=True)
        self._thread.start()

    def attach_cpu(self, cpu):
        self._cpu = cpu

    def write(self, value):
        sys.stdout.write(chr(value & 0xFF))
        sys.stdout.flush()

    def read_status(self):
        # bit0: output ready, bit1: input available
        status = 0x01
        if not self._input_q.empty():
            status |= 0x02
        return status

    def read_input(self):
        try:
            return self._input_q.get_nowait()
        except queue.Empty:
            return 0x00

    def stop(self):
        self._stop_event.set()

    def _input_loop(self):
        while not self._stop_event.is_set():
            try:
                line = sys.stdin.readline()
                if line == "":
                    time.sleep(0.05)
                    continue

                cmd = line.strip().lower()

                if cmd == "&exit":
                    if self._cpu is not None:
                        self._cpu.request_stop()
                    self.stop()
                    return

                if cmd == "&pause":
                    if self._cpu is not None:
                        self._cpu.request_pause()
                    continue

                if cmd == "&unpause":
                    if self._cpu is not None:
                        self._cpu.request_resume()
                    continue

                # Normal keyboard text: queue the characters plus newline
                for ch in line:
                    self._input_q.put(ord(ch) & 0xFF)

            except Exception:
                time.sleep(0.05)



# NASM source loader  (assembles a tiny subset at load time, no nasm binary)
# Supports: LDA/LDX/LDY/STA/STX/STY/ADC/SBC/AND/ORA/EOR/CMP/CPX/CPY
#           INC/DEC/INX/INY/DEX/DEY/TAX/TAY/TXA/TYA/TXS/TSX
#           PHA/PLA/PHP/PLP/NOP/BRK/RTS/RTI/JSR/JMP
#           Branches: BEQ/BNE/BCC/BCS/BMI/BPL/BVC/BVS
#           ASL/LSR/ROL/ROR  (accumulator and zero-page)
#           Addressing: #imm, abs, zp, abs,X abs,Y zp,X zp,Y (ind,X) (ind),Y
# Labels, EQU/= defines, ORG directive, DB byte literals

class NASMLoader:
    """
    Parses a NASM-syntax 6502 assembly source file and loads the
    resulting bytes directly into an emu6502 memory bytearray.
    No external assembler is required.
    """

    OPCODES = {
        # (mnemonic, mode) -> opcode byte
        # Modes: imm zp zpx zpy abs absx absy indx indy acc imp
        ('LDA','imm'):0xA9, ('LDA','zp'):0xA5,  ('LDA','zpx'):0xB5,
        ('LDA','abs'):0xAD, ('LDA','absx'):0xBD, ('LDA','absy'):0xB9,
        ('LDA','indx'):0xA1,('LDA','indy'):0xB1,

        ('LDX','imm'):0xA2, ('LDX','zp'):0xA6,  ('LDX','zpy'):0xB6,
        ('LDX','abs'):0xAE, ('LDX','absy'):0xBE,

        ('LDY','imm'):0xA0, ('LDY','zp'):0xA4,  ('LDY','zpx'):0xB4,
        ('LDY','abs'):0xAC, ('LDY','absx'):0xBC,

        ('STA','zp'):0x85,  ('STA','zpx'):0x95,
        ('STA','abs'):0x8D, ('STA','absx'):0x9D, ('STA','absy'):0x99,
        ('STA','indx'):0x81,('STA','indy'):0x91,

        ('STX','zp'):0x86,  ('STX','zpy'):0x96,  ('STX','abs'):0x8E,
        ('STY','zp'):0x84,  ('STY','zpx'):0x94,  ('STY','abs'):0x8C,

        ('ADC','imm'):0x69, ('ADC','zp'):0x65,  ('ADC','zpx'):0x75,
        ('ADC','abs'):0x6D, ('ADC','absx'):0x7D, ('ADC','absy'):0x79,
        ('ADC','indx'):0x61,('ADC','indy'):0x71,

        ('SBC','imm'):0xE9, ('SBC','zp'):0xE5,  ('SBC','zpx'):0xF5,
        ('SBC','abs'):0xED, ('SBC','absx'):0xFD, ('SBC','absy'):0xF9,
        ('SBC','indx'):0xE1,('SBC','indy'):0xF1,

        ('AND','imm'):0x29, ('AND','zp'):0x25,  ('AND','zpx'):0x35,
        ('AND','abs'):0x2D, ('AND','absx'):0x3D, ('AND','absy'):0x39,
        ('AND','indx'):0x21,('AND','indy'):0x31,

        ('ORA','imm'):0x09, ('ORA','zp'):0x05,  ('ORA','zpx'):0x15,
        ('ORA','abs'):0x0D, ('ORA','absx'):0x1D, ('ORA','absy'):0x19,
        ('ORA','indx'):0x01,('ORA','indy'):0x11,

        ('EOR','imm'):0x49, ('EOR','zp'):0x45,  ('EOR','zpx'):0x55,
        ('EOR','abs'):0x4D, ('EOR','absx'):0x5D, ('EOR','absy'):0x59,
        ('EOR','indx'):0x41,('EOR','indy'):0x51,

        ('CMP','imm'):0xC9, ('CMP','zp'):0xC5,  ('CMP','zpx'):0xD5,
        ('CMP','abs'):0xCD, ('CMP','absx'):0xDD, ('CMP','absy'):0xD9,
        ('CMP','indx'):0xC1,('CMP','indy'):0xD1,

        ('CPX','imm'):0xE0, ('CPX','zp'):0xE4,  ('CPX','abs'):0xEC,
        ('CPY','imm'):0xC0, ('CPY','zp'):0xC4,  ('CPY','abs'):0xCC,

        ('INC','zp'):0xE6,  ('INC','zpx'):0xF6,  ('INC','abs'):0xEE, ('INC','absx'):0xFE,
        ('DEC','zp'):0xC6,  ('DEC','zpx'):0xD6,  ('DEC','abs'):0xCE, ('DEC','absx'):0xDE,

        ('ASL','acc'):0x0A, ('ASL','zp'):0x06,  ('ASL','zpx'):0x16,
        ('ASL','abs'):0x0E, ('ASL','absx'):0x1E,
        ('LSR','acc'):0x4A, ('LSR','zp'):0x46,  ('LSR','zpx'):0x56,
        ('LSR','abs'):0x4E, ('LSR','absx'):0x5E,
        ('ROL','acc'):0x2A, ('ROL','zp'):0x26,  ('ROL','zpx'):0x36,
        ('ROL','abs'):0x2E, ('ROL','absx'):0x3E,
        ('ROR','acc'):0x6A, ('ROR','zp'):0x66,  ('ROR','zpx'):0x76,
        ('ROR','abs'):0x6E, ('ROR','absx'):0x7E,

        ('BIT','zp'):0x24,  ('BIT','abs'):0x2C,

        ('JMP','abs'):0x4C, ('JMP','ind'):0x6C,
        ('JSR','abs'):0x20,

        # Branches (rel) – handled specially
        ('BEQ','rel'):0xF0, ('BNE','rel'):0xD0,
        ('BCC','rel'):0x90, ('BCS','rel'):0xB0,
        ('BMI','rel'):0x30, ('BPL','rel'):0x10,
        ('BVC','rel'):0x50, ('BVS','rel'):0x70,

        # Implied / accumulator single-byte
        ('NOP','imp'):0xEA,
        ('BRK','imp'):0x00,
        ('RTS','imp'):0x60,
        ('RTI','imp'):0x40,
        ('PHA','imp'):0x48, ('PLA','imp'):0x68,
        ('PHP','imp'):0x08, ('PLP','imp'):0x28,
        ('TAX','imp'):0xAA, ('TXA','imp'):0x8A,
        ('TAY','imp'):0xA8, ('TYA','imp'):0x98,
        ('TXS','imp'):0x9A, ('TSX','imp'):0xBA,
        ('INX','imp'):0xE8, ('DEX','imp'):0xCA,
        ('INY','imp'):0xC8, ('DEY','imp'):0x88,
        ('CLC','imp'):0x18, ('SEC','imp'):0x38,
        ('CLI','imp'):0x58, ('SEI','imp'):0x78,
        ('CLV','imp'):0xB8, ('CLD','imp'):0xD8, ('SED','imp'):0xF8,
    }

    BRANCH_MNEMONICS = {'BEQ','BNE','BCC','BCS','BMI','BPL','BVC','BVS'}
    IMPLIED = {'NOP','BRK','RTS','RTI','PHA','PLA','PHP','PLP',
               'TAX','TXA','TAY','TYA','TXS','TSX',
               'INX','DEX','INY','DEY',
               'CLC','SEC','CLI','SEI','CLV','CLD','SED'}

    def __init__(self):
        self.labels  = {}   # name -> address
        self.defines = {}   # name -> int value
        self.patches = []   # (addr, label, is_branch, instr_end_addr)

    def _norm_symbol(self, s):
      return s.strip().strip("'\"")

    def _parse_int(self, token):
        token = self._norm_symbol(str(token))
    
        if not token:
            raise ValueError("Empty expression")
    
        if token[0] in "<>" and token not in ("<", ">"):
            hi = token[0] == ">"
            inner = token[1:].strip()
            value = self._parse_int(inner)
            return ((value >> 8) & 0xFF) if hi else (value & 0xFF)
    
        m = re.fullmatch(r"(.+?)([+-])(.+)", token)
        if m:
            left_txt = m.group(1).strip()
            op = m.group(2)
            right_txt = m.group(3).strip()
            left = self._parse_int(left_txt)
            right = self._parse_int(right_txt)
            return left + right if op == "+" else left - right
    
        if token in self.defines:
            return self.defines[token]
        if token in self.labels:
            return self.labels[token]
    
        if token.startswith("0x") or token.startswith("0X"):
            return int(token, 16)
        if token.endswith("h") or token.endswith("H"):
            return int(token[:-1], 16)
        if token.startswith("$"):
            return int(token[1:], 16)
        if token.startswith("%"):
            return int(token[1:], 2)
    
        if re.fullmatch(r"[A-Za-z_]\w*", token):
            raise KeyError(token)
    
        return int(token, 10)

    def _parse_operand(self, mnem, operand, cur_addr):
        """
        Returns (mode, [bytes...]) or raises ValueError.
        bytes does NOT include the opcode itself.
        """
        operand = operand.strip()

        # Accumulator explicit
        if operand.upper() in ('A', ''):
            if mnem in ('ASL','LSR','ROL','ROR'):
                return 'acc', []
            if operand == '' and mnem in self.IMPLIED:
                return 'imp', []

        # Immediate  #value
        m = re.fullmatch(r'#\s*(.+)', operand)
        if m:
            expr = m.group(1).strip()

            try:
                val = self._parse_int(expr)
                return 'imm', [val & 0xFF]
            except (ValueError, KeyError):
                pass

            # Support forward-referenced #<label and #>label
            lo_hi = re.fullmatch(r'([<>])\s*(.+)', expr)
            if lo_hi:
                kind = 'lo' if lo_hi.group(1) == '<' else 'hi'
                symbol = lo_hi.group(2).strip()
                self.patches.append((cur_addr + 1, symbol, kind, False, cur_addr + 2))
                return 'imm', [0x00]

            raise ValueError(f"Invalid immediate operand: {operand!r}")

        # Indirect X  (zp,X)
        m = re.fullmatch(r'\((.+),\s*[Xx]\)', operand)
        if m:
            val = self._parse_int(m.group(1))
            return 'indx', [val & 0xFF]

        # Indirect Y  (zp),Y
        m = re.fullmatch(r'\((.+)\),\s*[Yy]', operand)
        if m:
            val = self._parse_int(m.group(1))
            return 'indy', [val & 0xFF]

        # Indirect abs  (abs)  – only JMP
        m = re.fullmatch(r'\((.+)\)', operand)
        if m:
            val = self._parse_int(m.group(1))
            return 'ind', [val & 0xFF, (val >> 8) & 0xFF]

        # Absolute or ZP with X index
        m = re.fullmatch(r'(.+),\s*[Xx]', operand)
        if m:
            expr = self._norm_symbol(m.group(1))
            try:
                val = self._parse_int(expr)
            except (ValueError, KeyError):
                self.patches.append((cur_addr + 1, expr, None, False, cur_addr + 3))
                return 'absx', [0x00, 0x00]
            if val <= 0xFF:
                return 'zpx', [val & 0xFF]
            return 'absx', [val & 0xFF, (val >> 8) & 0xFF]

        m = re.fullmatch(r'(.+),\s*[Yy]', operand)
        if m:
            expr = self._norm_symbol(m.group(1))
            try:
                val = self._parse_int(expr)
            except (ValueError, KeyError):
                self.patches.append((cur_addr + 1, expr, None, False, cur_addr + 3))
                return 'absy', [0x00, 0x00]
            if val <= 0xFF:
                return 'zpy', [val & 0xFF]
            return 'absy', [val & 0xFF, (val >> 8) & 0xFF]

        # Branches – label reference (relative)
        if mnem in self.BRANCH_MNEMONICS:
            op = self._norm_symbol(operand)

            # Only treat literal numeric values as direct offsets.
            # Labels must always be patched relative to the branch location.
            is_numeric = bool(re.fullmatch(
                r'[+-]?(?:'
                r'0x[0-9A-Fa-f]+|'   # hex 0x10
                r'\$[0-9A-Fa-f]+|'   # hex $10
                r'%[01]+|'           # binary %1010
                r'\d+|'              # decimal 10
                r'[0-9A-Fa-f]+[hH]'  # hex 10h
                r')',
                op
            ))

            if is_numeric:
                val = self._parse_int(op)
                return 'rel', [val & 0xFF]

            # label -> patch relative later
            self.patches.append((cur_addr + 1, op, None, True, cur_addr + 2))
            return 'rel', [0x00]
        # Bare label or address (abs/zp/jsr/jmp)
        try:
            val = self._parse_int(operand)
        except (ValueError, KeyError):
            self.patches.append((cur_addr + 1, operand, None, False, cur_addr + 3))
            return 'abs', [0x00, 0x00]

        if val <= 0xFF and mnem not in ('JMP','JSR'):
            return 'zp', [val & 0xFF]
        return 'abs', [val & 0xFF, (val >> 8) & 0xFF]

    def split_db(self, text):
      parts = []
      current = ""
      in_str = False
      quote = None
  
      for c in text:
          if c in "\"'" and not in_str:
              in_str = True
              quote = c
              current += c
          elif c == quote and in_str:
              in_str = False
              current += c
          elif c == ',' and not in_str:
              parts.append(current.strip())
              current = ""
          else:
              current += c
  
      if current.strip():
          parts.append(current.strip())
  
      return parts
  
    def assemble(self, source):
        """Assemble NASM 6502 source and return a list of (address, byte) tuples."""
        output = []
        org = 0x0000
        cur = org
    
        lines = source.splitlines()
    
        # ---- Pass 1: emit bytes, collect labels, record patches ----
        for lineno, raw in enumerate(lines, 1):
            # strip comments and whitespace
            line = raw.split(';', 1)[0].strip()
            if not line:
                continue
    
            # Label prefix on the same line:
            #   label:
            #   label: INSTR ...
            m = re.match(r'^\s*([A-Za-z_]\w*)\s*:\s*(.*)$', line)
            if m:
                self.labels[self._norm_symbol(m.group(1))] = cur
                line = m.group(2).strip()
                if not line:
                    continue
    
            # ORG directive
            m = re.match(r'^ORG\s+(.+)$', line, re.IGNORECASE)
            if m:
                cur = self._parse_int(m.group(1).strip())
                org = cur
                continue
    
            # EQU / define  (name EQU value  or  name = value)
            m = re.match(r'^([A-Za-z_]\w*)\s+(?:EQU|=)\s+(.+)$', line, re.IGNORECASE)
            if m:
                self.defines[self._norm_symbol(m.group(1))] = self._parse_int(m.group(2).strip())
                continue
    
            # DB / DW data  (with optional leading label)
            m = re.match(r'^(DB|DW)\s+(.+)$', line, re.IGNORECASE)
            if m:
                directive = m.group(1).upper()
                data = m.group(2)
    
                parts = self.split_db(data)
                for part in parts:
                    part = part.strip()
                    if not part:
                        continue
    
                    if len(part) >= 2 and part[0] in "\"'" and part[-1] == part[0]:
                        for ch in part[1:-1]:
                            output.append((cur, ord(ch)))
                            cur += 1
                    else:
                        val = self._parse_int(part)
                        if directive == 'DW':
                            output.append((cur, val & 0xFF))
                            cur += 1
                            output.append((cur, (val >> 8) & 0xFF))
                            cur += 1
                        else:
                            output.append((cur, val & 0xFF))
                            cur += 1
                continue
    
            # Standalone label on its own line  e.g.  "start:"
            m = re.match(r'^\s*([A-Za-z_]\w*)\s*:\s*$', line)
            if m:
                self.labels[self._norm_symbol(m.group(1))] = cur
                continue
    
            # Label prefix on an instruction line  e.g.  "start: LDA #0"
            m = re.match(r'^\s*([A-Za-z_]\w*)\s*:\s+(\S.*)$', line)
            if m:
                self.labels[self._norm_symbol(m.group(1))] = cur
                line = m.group(2).strip()
    
            # Mnemonic [operand]
            parts = line.split(None, 1)
            mnem = parts[0].upper()
            operand = parts[1].strip() if len(parts) > 1 else ''
    
            if mnem in self.IMPLIED or (operand == '' and mnem not in self.BRANCH_MNEMONICS):
                mode = 'imp' if mnem in self.IMPLIED else 'acc'
                if mnem in ('ASL', 'LSR', 'ROL', 'ROR') and operand.upper() in ('A', ''):
                    mode = 'acc'
                key = (mnem, mode)
                if key not in self.OPCODES:
                    raise ValueError(f"Line {lineno}: unknown implied opcode for {mnem}")
                output.append((cur, self.OPCODES[key]))
                cur += 1
                continue
    
            mode, extra_bytes = self._parse_operand(mnem, operand, cur)
            key = (mnem, mode)
            if key not in self.OPCODES:
                raise ValueError(f"Line {lineno}: no opcode for {mnem} {mode}  (operand={operand!r})")
            output.append((cur, self.OPCODES[key]))
            cur += 1
            for b in extra_bytes:
                output.append((cur, b & 0xFF))
                cur += 1
    
        if DEBUG:
            print("LABELS:", self.labels)
            print("PATCHES:", self.patches)
    
        # ---- Pass 2: resolve patches ----
        addr_map = {addr: i for i, (addr, _) in enumerate(output)}
    
        for patch_addr, symbol, patch_kind, is_branch, instr_end in self.patches:
            symbol = self._norm_symbol(symbol)
            if symbol not in self.labels:
                raise ValueError(f"Undefined label: {symbol!r}")
    
            target = self.labels[symbol]
    
            if is_branch:
                offset = target - instr_end
                if offset < -128 or offset > 127:
                    raise ValueError(f"Branch to {symbol!r} out of range ({offset})")
                idx = addr_map.get(patch_addr)
                if idx is not None:
                    output[idx] = (output[idx][0], offset & 0xFF)
                continue
    
            if patch_kind in ('lo', 'hi'):
                value = (target & 0xFF) if patch_kind == 'lo' else ((target >> 8) & 0xFF)
                idx = addr_map.get(patch_addr)
                if idx is not None:
                    output[idx] = (output[idx][0], value)
                continue
    
            idx = addr_map.get(patch_addr)
            if idx is not None:
                output[idx] = (output[idx][0], target & 0xFF)
            idx2 = addr_map.get(patch_addr + 1)
            if idx2 is not None:
                output[idx2] = (output[idx2][0], (target >> 8) & 0xFF)
    
        return output
  
    def load_file(self, path, memory, pc_override=None):
        """
        Read a NASM source file, assemble it, write bytes into memory.
        Returns the ORG/start address (or pc_override if supplied).
        """
        with open(path, 'r') as f:
            source = f.read()
        return self.load_string(source, memory, pc_override)

    def load_string(self, source, memory, pc_override=None):
        """
        Assemble source string and write bytes into memory bytearray.
        Returns the start address.
        """
        self.labels  = {}
        self.defines = {}
        self.patches = []
        pairs = self.assemble(source)
        if not pairs:
            return pc_override or 0x0000
        start = pairs[0][0]
        for addr, byte in pairs:
            memory[addr & 0xFFFF] = byte & 0xFF
        return pc_override if pc_override is not None else start



# 6502 CPU emulator

class emu6502:
    # Flags
    FLAG_CARRY     = 0b00000001
    FLAG_ZERO      = 0b00000010
    FLAG_INTERRUPT = 0b00000100
    FLAG_DECIMAL   = 0b00001000
    FLAG_BREAK     = 0b00010000
    FLAG_UNUSED    = 0b00100000
    FLAG_OVERFLOW  = 0b01000000
    FLAG_NEGATIVE  = 0b10000000

    def __init__(self):
        # Registers
        self.A      = 0x00
        self.X      = 0x00
        self.Y      = 0x00
        self.SP     = 0xFD
        self.PC     = 0x0000
        self.STATUS = self.FLAG_UNUSED
        self.memory = bytearray(65536)
        self.running = True
        self.paused = False
        self.uart   = UART()
        self.uart.attach_cpu(self)
        self.cycles  = 0
        print("6502 Emulator has been initialized.")

    # ------------------------------------------------------------------
    # Flag helpers
    # ------------------------------------------------------------------
    def set_flag(self, flag, value):
        if value:
            self.STATUS |= flag
        else:
            self.STATUS &= ~flag

    def get_flag(self, flag):
        return (self.STATUS & flag) != 0

    def update_zero_negative(self, value):
        value &= 0xFF
        self.set_flag(self.FLAG_ZERO,     value == 0)
        self.set_flag(self.FLAG_NEGATIVE, (value & 0x80) != 0)

    # ------------------------------------------------------------------
    # Memory-mapped I/O aware read/write
    # ------------------------------------------------------------------
    def read8(self, addr):
        addr &= 0xFFFF
        if addr == UART_STATUS_ADDR:
            return self.uart.read_status()
        if addr == UART_INPUT_ADDR:
            return self.uart.read_input()
        return self.memory[addr]

    def write8(self, addr, value):
        addr  &= 0xFFFF
        value &= 0xFF
        if addr == UART_DATA_ADDR:
            self.uart.write(value)
            return
        self.memory[addr] = value

    def fetch_byte(self):
        value  = self.read8(self.PC)
        self.PC = (self.PC + 1) & 0xFFFF
        return value

    def fetch_word(self):
        lo = self.fetch_byte()
        hi = self.fetch_byte()
        return lo | (hi << 8)

    def read16(self, addr):
        lo = self.read8(addr)
        hi = self.read8((addr + 1) & 0xFFFF)
        return lo | (hi << 8)

    # 6502 page-wrap bug for (zp),Y and (zp,X)
    def read16_zp(self, addr):
        addr &= 0xFF
        lo = self.read8(addr)
        hi = self.read8((addr + 1) & 0xFF)
        return lo | (hi << 8)

    # ------------------------------------------------------------------
    # Stack helpers
    # ------------------------------------------------------------------
    def push(self, value):
        self.write8(0x0100 | self.SP, value & 0xFF)
        self.SP = (self.SP - 1) & 0xFF

    def pop(self):
        self.SP = (self.SP + 1) & 0xFF
        return self.read8(0x0100 | self.SP)

    # Uart - Input / Output

    def request_stop(self):
      self.running = False
      self.paused = False
  
    def request_pause(self):
        self.paused = True
    
    def request_resume(self):
        self.paused = False
  
    # ------------------------------------------------------------------
    # Addressing mode helpers – return effective address
    # ------------------------------------------------------------------
    def addr_zp(self):
        return self.fetch_byte()

    def addr_zpx(self):
        return (self.fetch_byte() + self.X) & 0xFF

    def addr_zpy(self):
        return (self.fetch_byte() + self.Y) & 0xFF

    def addr_abs(self):
        return self.fetch_word()

    def addr_absx(self):
        base = self.fetch_word()
        return (base + self.X) & 0xFFFF

    def addr_absy(self):
        base = self.fetch_word()
        return (base + self.Y) & 0xFFFF

    def addr_indx(self):
        zp = (self.fetch_byte() + self.X) & 0xFF
        return self.read16_zp(zp)

    def addr_indy(self):
        zp   = self.fetch_byte()
        base = self.read16_zp(zp)
        return (base + self.Y) & 0xFFFF

    # ------------------------------------------------------------------
    # ADC / SBC shared arithmetic
    # ------------------------------------------------------------------
    def _adc(self, value):
        carry  = 1 if self.get_flag(self.FLAG_CARRY) else 0
        result = self.A + value + carry
        # Overflow: both operands same sign, result different sign
        self.set_flag(self.FLAG_OVERFLOW,
                      (~(self.A ^ value) & (self.A ^ result) & 0x80) != 0)
        self.set_flag(self.FLAG_CARRY, result > 0xFF)
        self.A = result & 0xFF
        self.update_zero_negative(self.A)

    def _sbc(self, value):
        self._adc(value ^ 0xFF)   # SBC = ADC with inverted operand

    # ------------------------------------------------------------------
    # Compare helper
    # ------------------------------------------------------------------
    def _cmp(self, reg, value):
        result = (reg - value) & 0xFF
        self.set_flag(self.FLAG_CARRY,    reg >= value)
        self.set_flag(self.FLAG_ZERO,     reg == value)
        self.set_flag(self.FLAG_NEGATIVE, (result & 0x80) != 0)

    # ------------------------------------------------------------------
    # Branch helper
    # ------------------------------------------------------------------
    def _branch(self, condition):
        offset = self.fetch_byte()
        if condition:
            if offset & 0x80:
                offset -= 256
            self.PC = (self.PC + offset) & 0xFFFF

    # ------------------------------------------------------------------
    # Shift / rotate helpers
    # ------------------------------------------------------------------
    def _asl(self, value):
        self.set_flag(self.FLAG_CARRY, (value & 0x80) != 0)
        result = (value << 1) & 0xFF
        self.update_zero_negative(result)
        return result

    def _lsr(self, value):
        self.set_flag(self.FLAG_CARRY, (value & 0x01) != 0)
        result = (value >> 1) & 0xFF
        self.update_zero_negative(result)
        return result

    def _rol(self, value):
        carry_in = 1 if self.get_flag(self.FLAG_CARRY) else 0
        self.set_flag(self.FLAG_CARRY, (value & 0x80) != 0)
        result = ((value << 1) | carry_in) & 0xFF
        self.update_zero_negative(result)
        return result

    def _ror(self, value):
        carry_in = 0x80 if self.get_flag(self.FLAG_CARRY) else 0
        self.set_flag(self.FLAG_CARRY, (value & 0x01) != 0)
        result = ((value >> 1) | carry_in) & 0xFF
        self.update_zero_negative(result)
        return result

    # ------------------------------------------------------------------
    # LDA
    # ------------------------------------------------------------------
    def LDA_immediate(self):
        self.A = self.fetch_byte(); self.update_zero_negative(self.A)
    def LDA_zp(self):
        self.A = self.read8(self.addr_zp()); self.update_zero_negative(self.A)
    def LDA_zpx(self):
        self.A = self.read8(self.addr_zpx()); self.update_zero_negative(self.A)
    def LDA_abs(self):
        self.A = self.read8(self.addr_abs()); self.update_zero_negative(self.A)
    def LDA_absx(self):
        self.A = self.read8(self.addr_absx()); self.update_zero_negative(self.A)
    def LDA_absy(self):
        self.A = self.read8(self.addr_absy()); self.update_zero_negative(self.A)
    def LDA_indx(self):
        self.A = self.read8(self.addr_indx()); self.update_zero_negative(self.A)
    def LDA_indy(self):
        self.A = self.read8(self.addr_indy()); self.update_zero_negative(self.A)

    # ------------------------------------------------------------------
    # LDX
    # ------------------------------------------------------------------
    def LDX_immediate(self):
        self.X = self.fetch_byte(); self.update_zero_negative(self.X)
    def LDX_zp(self):
        self.X = self.read8(self.addr_zp()); self.update_zero_negative(self.X)
    def LDX_zpy(self):
        self.X = self.read8(self.addr_zpy()); self.update_zero_negative(self.X)
    def LDX_abs(self):
        self.X = self.read8(self.addr_abs()); self.update_zero_negative(self.X)
    def LDX_absy(self):
        self.X = self.read8(self.addr_absy()); self.update_zero_negative(self.X)

    # ------------------------------------------------------------------
    # LDY
    # ------------------------------------------------------------------
    def LDY_immediate(self):
        self.Y = self.fetch_byte(); self.update_zero_negative(self.Y)
    def LDY_zp(self):
        self.Y = self.read8(self.addr_zp()); self.update_zero_negative(self.Y)
    def LDY_zpx(self):
        self.Y = self.read8(self.addr_zpx()); self.update_zero_negative(self.Y)
    def LDY_abs(self):
        self.Y = self.read8(self.addr_abs()); self.update_zero_negative(self.Y)
    def LDY_absx(self):
        self.Y = self.read8(self.addr_absx()); self.update_zero_negative(self.Y)

    # ------------------------------------------------------------------
    # STA / STX / STY
    # ------------------------------------------------------------------
    def STA_zp(self):   self.write8(self.addr_zp(),   self.A)
    def STA_zpx(self):  self.write8(self.addr_zpx(),  self.A)
    def STA_abs(self):  self.write8(self.addr_abs(),  self.A)
    def STA_absx(self): self.write8(self.addr_absx(), self.A)
    def STA_absy(self): self.write8(self.addr_absy(), self.A)
    def STA_indx(self): self.write8(self.addr_indx(), self.A)
    def STA_indy(self): self.write8(self.addr_indy(), self.A)

    def STX_zp(self):   self.write8(self.addr_zp(),  self.X)
    def STX_zpy(self):  self.write8(self.addr_zpy(), self.X)
    def STX_abs(self):  self.write8(self.addr_abs(), self.X)

    def STY_zp(self):   self.write8(self.addr_zp(),  self.Y)
    def STY_zpx(self):  self.write8(self.addr_zpx(), self.Y)
    def STY_abs(self):  self.write8(self.addr_abs(), self.Y)

    # ------------------------------------------------------------------
    # ADC
    # ------------------------------------------------------------------
    def ADC_immediate(self): self._adc(self.fetch_byte())
    def ADC_zp(self):        self._adc(self.read8(self.addr_zp()))
    def ADC_zpx(self):       self._adc(self.read8(self.addr_zpx()))
    def ADC_abs(self):       self._adc(self.read8(self.addr_abs()))
    def ADC_absx(self):      self._adc(self.read8(self.addr_absx()))
    def ADC_absy(self):      self._adc(self.read8(self.addr_absy()))
    def ADC_indx(self):      self._adc(self.read8(self.addr_indx()))
    def ADC_indy(self):      self._adc(self.read8(self.addr_indy()))

    # ------------------------------------------------------------------
    # SBC
    # ------------------------------------------------------------------
    def SBC_immediate(self): self._sbc(self.fetch_byte())
    def SBC_zp(self):        self._sbc(self.read8(self.addr_zp()))
    def SBC_zpx(self):       self._sbc(self.read8(self.addr_zpx()))
    def SBC_abs(self):       self._sbc(self.read8(self.addr_abs()))
    def SBC_absx(self):      self._sbc(self.read8(self.addr_absx()))
    def SBC_absy(self):      self._sbc(self.read8(self.addr_absy()))
    def SBC_indx(self):      self._sbc(self.read8(self.addr_indx()))
    def SBC_indy(self):      self._sbc(self.read8(self.addr_indy()))

    # ------------------------------------------------------------------
    # AND
    # ------------------------------------------------------------------
    def AND_immediate(self): self.A &= self.fetch_byte();             self.update_zero_negative(self.A)
    def AND_zp(self):        self.A &= self.read8(self.addr_zp());    self.update_zero_negative(self.A)
    def AND_zpx(self):       self.A &= self.read8(self.addr_zpx());   self.update_zero_negative(self.A)
    def AND_abs(self):       self.A &= self.read8(self.addr_abs());   self.update_zero_negative(self.A)
    def AND_absx(self):      self.A &= self.read8(self.addr_absx());  self.update_zero_negative(self.A)
    def AND_absy(self):      self.A &= self.read8(self.addr_absy());  self.update_zero_negative(self.A)
    def AND_indx(self):      self.A &= self.read8(self.addr_indx());  self.update_zero_negative(self.A)
    def AND_indy(self):      self.A &= self.read8(self.addr_indy());  self.update_zero_negative(self.A)

    # ------------------------------------------------------------------
    # ORA
    # ------------------------------------------------------------------
    def ORA_immediate(self): self.A |= self.fetch_byte();             self.update_zero_negative(self.A)
    def ORA_zp(self):        self.A |= self.read8(self.addr_zp());    self.update_zero_negative(self.A)
    def ORA_zpx(self):       self.A |= self.read8(self.addr_zpx());   self.update_zero_negative(self.A)
    def ORA_abs(self):       self.A |= self.read8(self.addr_abs());   self.update_zero_negative(self.A)
    def ORA_absx(self):      self.A |= self.read8(self.addr_absx());  self.update_zero_negative(self.A)
    def ORA_absy(self):      self.A |= self.read8(self.addr_absy());  self.update_zero_negative(self.A)
    def ORA_indx(self):      self.A |= self.read8(self.addr_indx());  self.update_zero_negative(self.A)
    def ORA_indy(self):      self.A |= self.read8(self.addr_indy());  self.update_zero_negative(self.A)

    # ------------------------------------------------------------------
    # EOR
    # ------------------------------------------------------------------
    def EOR_immediate(self): self.A ^= self.fetch_byte();             self.update_zero_negative(self.A)
    def EOR_zp(self):        self.A ^= self.read8(self.addr_zp());    self.update_zero_negative(self.A)
    def EOR_zpx(self):       self.A ^= self.read8(self.addr_zpx());   self.update_zero_negative(self.A)
    def EOR_abs(self):       self.A ^= self.read8(self.addr_abs());   self.update_zero_negative(self.A)
    def EOR_absx(self):      self.A ^= self.read8(self.addr_absx());  self.update_zero_negative(self.A)
    def EOR_absy(self):      self.A ^= self.read8(self.addr_absy());  self.update_zero_negative(self.A)
    def EOR_indx(self):      self.A ^= self.read8(self.addr_indx());  self.update_zero_negative(self.A)
    def EOR_indy(self):      self.A ^= self.read8(self.addr_indy());  self.update_zero_negative(self.A)

    # ------------------------------------------------------------------
    # CMP / CPX / CPY
    # ------------------------------------------------------------------
    def CMP_immediate(self): self._cmp(self.A, self.fetch_byte())
    def CMP_zp(self):        self._cmp(self.A, self.read8(self.addr_zp()))
    def CMP_zpx(self):       self._cmp(self.A, self.read8(self.addr_zpx()))
    def CMP_abs(self):       self._cmp(self.A, self.read8(self.addr_abs()))
    def CMP_absx(self):      self._cmp(self.A, self.read8(self.addr_absx()))
    def CMP_absy(self):      self._cmp(self.A, self.read8(self.addr_absy()))
    def CMP_indx(self):      self._cmp(self.A, self.read8(self.addr_indx()))
    def CMP_indy(self):      self._cmp(self.A, self.read8(self.addr_indy()))

    def CPX_immediate(self): self._cmp(self.X, self.fetch_byte())
    def CPX_zp(self):        self._cmp(self.X, self.read8(self.addr_zp()))
    def CPX_abs(self):       self._cmp(self.X, self.read8(self.addr_abs()))

    def CPY_immediate(self): self._cmp(self.Y, self.fetch_byte())
    def CPY_zp(self):        self._cmp(self.Y, self.read8(self.addr_zp()))
    def CPY_abs(self):       self._cmp(self.Y, self.read8(self.addr_abs()))

    # ------------------------------------------------------------------
    # INC / DEC
    # ------------------------------------------------------------------
    def INC_zp(self):
        a = self.addr_zp();   v = (self.read8(a)+1)&0xFF; self.write8(a,v); self.update_zero_negative(v)
    def INC_zpx(self):
        a = self.addr_zpx();  v = (self.read8(a)+1)&0xFF; self.write8(a,v); self.update_zero_negative(v)
    def INC_abs(self):
        a = self.addr_abs();  v = (self.read8(a)+1)&0xFF; self.write8(a,v); self.update_zero_negative(v)
    def INC_absx(self):
        a = self.addr_absx(); v = (self.read8(a)+1)&0xFF; self.write8(a,v); self.update_zero_negative(v)

    def DEC_zp(self):
        a = self.addr_zp();   v = (self.read8(a)-1)&0xFF; self.write8(a,v); self.update_zero_negative(v)
    def DEC_zpx(self):
        a = self.addr_zpx();  v = (self.read8(a)-1)&0xFF; self.write8(a,v); self.update_zero_negative(v)
    def DEC_abs(self):
        a = self.addr_abs();  v = (self.read8(a)-1)&0xFF; self.write8(a,v); self.update_zero_negative(v)
    def DEC_absx(self):
        a = self.addr_absx(); v = (self.read8(a)-1)&0xFF; self.write8(a,v); self.update_zero_negative(v)

    # ------------------------------------------------------------------
    # Register transfers / increments / decrements
    # ------------------------------------------------------------------
    def TAX(self):
        self.X = self.A; self.update_zero_negative(self.X)
    def TXA(self):
        self.A = self.X; self.update_zero_negative(self.A)
    def TAY(self):
        self.Y = self.A; self.update_zero_negative(self.Y)
    def TYA(self):
        self.A = self.Y; self.update_zero_negative(self.A)
    def TXS(self):
        self.SP = self.X
    def TSX(self):
        self.X = self.SP; self.update_zero_negative(self.X)
    def INX(self):
        self.X = (self.X+1)&0xFF; self.update_zero_negative(self.X)
    def INY(self):
        self.Y = (self.Y+1)&0xFF; self.update_zero_negative(self.Y)
    def DEX(self):
        self.X = (self.X-1)&0xFF; self.update_zero_negative(self.X)
    def DEY(self):
        self.Y = (self.Y-1)&0xFF; self.update_zero_negative(self.Y)

    # ------------------------------------------------------------------
    # ASL / LSR / ROL / ROR  (accumulator and memory)
    # ------------------------------------------------------------------
    def ASL_acc(self):  self.A = self._asl(self.A)
    def ASL_zp(self):   a=self.addr_zp();   self.write8(a, self._asl(self.read8(a)))
    def ASL_zpx(self):  a=self.addr_zpx();  self.write8(a, self._asl(self.read8(a)))
    def ASL_abs(self):  a=self.addr_abs();  self.write8(a, self._asl(self.read8(a)))
    def ASL_absx(self): a=self.addr_absx(); self.write8(a, self._asl(self.read8(a)))

    def LSR_acc(self):  self.A = self._lsr(self.A)
    def LSR_zp(self):   a=self.addr_zp();   self.write8(a, self._lsr(self.read8(a)))
    def LSR_zpx(self):  a=self.addr_zpx();  self.write8(a, self._lsr(self.read8(a)))
    def LSR_abs(self):  a=self.addr_abs();  self.write8(a, self._lsr(self.read8(a)))
    def LSR_absx(self): a=self.addr_absx(); self.write8(a, self._lsr(self.read8(a)))

    def ROL_acc(self):  self.A = self._rol(self.A)
    def ROL_zp(self):   a=self.addr_zp();   self.write8(a, self._rol(self.read8(a)))
    def ROL_zpx(self):  a=self.addr_zpx();  self.write8(a, self._rol(self.read8(a)))
    def ROL_abs(self):  a=self.addr_abs();  self.write8(a, self._rol(self.read8(a)))
    def ROL_absx(self): a=self.addr_absx(); self.write8(a, self._rol(self.read8(a)))

    def ROR_acc(self):  self.A = self._ror(self.A)
    def ROR_zp(self):   a=self.addr_zp();   self.write8(a, self._ror(self.read8(a)))
    def ROR_zpx(self):  a=self.addr_zpx();  self.write8(a, self._ror(self.read8(a)))
    def ROR_abs(self):  a=self.addr_abs();  self.write8(a, self._ror(self.read8(a)))
    def ROR_absx(self): a=self.addr_absx(); self.write8(a, self._ror(self.read8(a)))

    # ------------------------------------------------------------------
    # BIT
    # ------------------------------------------------------------------
    def BIT_zp(self):
        v = self.read8(self.addr_zp())
        self.set_flag(self.FLAG_ZERO,     (self.A & v) == 0)
        self.set_flag(self.FLAG_NEGATIVE, (v & 0x80) != 0)
        self.set_flag(self.FLAG_OVERFLOW, (v & 0x40) != 0)

    def BIT_abs(self):
        v = self.read8(self.addr_abs())
        self.set_flag(self.FLAG_ZERO,     (self.A & v) == 0)
        self.set_flag(self.FLAG_NEGATIVE, (v & 0x80) != 0)
        self.set_flag(self.FLAG_OVERFLOW, (v & 0x40) != 0)

    # ------------------------------------------------------------------
    # JMP / JSR / RTS / RTI
    # ------------------------------------------------------------------
    def JMP_abs(self):
        self.PC = self.fetch_word()

    def JMP_ind(self):
        ptr = self.fetch_word()
        # 6502 page-crossing bug: high byte wraps within page
        lo = self.read8(ptr)
        hi = self.read8((ptr & 0xFF00) | ((ptr+1) & 0x00FF))
        self.PC = lo | (hi << 8)

    def JSR(self):
        addr = self.fetch_word()
        ret  = (self.PC - 1) & 0xFFFF
        self.push((ret >> 8) & 0xFF)
        self.push(ret & 0xFF)
        self.PC = addr

    def RTS(self):
        lo = self.pop()
        hi = self.pop()
        self.PC = ((lo | (hi << 8)) + 1) & 0xFFFF

    def RTI(self):
        self.STATUS = (self.pop() | self.FLAG_UNUSED) & ~self.FLAG_BREAK
        lo = self.pop()
        hi = self.pop()
        self.PC = lo | (hi << 8)

    # ------------------------------------------------------------------
    # BRK
    # ------------------------------------------------------------------
    def BRK(self):
        self.running = False
        self.set_flag(self.FLAG_BREAK, True)

    # ------------------------------------------------------------------
    # Stack
    # ------------------------------------------------------------------
    def PHA(self): self.push(self.A)
    def PLA(self): self.A = self.pop(); self.update_zero_negative(self.A)
    def PHP(self): self.push(self.STATUS | self.FLAG_BREAK)
    def PLP(self): self.STATUS = (self.pop() | self.FLAG_UNUSED) & ~self.FLAG_BREAK

    # ------------------------------------------------------------------
    # Branches
    # ------------------------------------------------------------------
    def BEQ(self): self._branch(self.get_flag(self.FLAG_ZERO))
    def BNE(self): self._branch(not self.get_flag(self.FLAG_ZERO))
    def BCC(self): self._branch(not self.get_flag(self.FLAG_CARRY))
    def BCS(self): self._branch(self.get_flag(self.FLAG_CARRY))
    def BMI(self): self._branch(self.get_flag(self.FLAG_NEGATIVE))
    def BPL(self): self._branch(not self.get_flag(self.FLAG_NEGATIVE))
    def BVC(self): self._branch(not self.get_flag(self.FLAG_OVERFLOW))
    def BVS(self): self._branch(self.get_flag(self.FLAG_OVERFLOW))

    # ------------------------------------------------------------------
    # Flag instructions
    # ------------------------------------------------------------------
    def CLC(self): self.set_flag(self.FLAG_CARRY,     False)
    def SEC(self): self.set_flag(self.FLAG_CARRY,     True)
    def CLI(self): self.set_flag(self.FLAG_INTERRUPT, False)
    def SEI(self): self.set_flag(self.FLAG_INTERRUPT, True)
    def CLV(self): self.set_flag(self.FLAG_OVERFLOW,  False)
    def CLD(self): self.set_flag(self.FLAG_DECIMAL,   False)
    def SED(self): self.set_flag(self.FLAG_DECIMAL,   True)

    # ------------------------------------------------------------------
    # NOP
    # ------------------------------------------------------------------
    def NOP(self): pass

    # ------------------------------------------------------------------
    # Dispatch table (built once)
    # ------------------------------------------------------------------
    def _build_dispatch(self):
        t = [None] * 256
        t[0x00] = self.BRK

        # LDA
        t[0xA9]=self.LDA_immediate; t[0xA5]=self.LDA_zp;   t[0xB5]=self.LDA_zpx
        t[0xAD]=self.LDA_abs;       t[0xBD]=self.LDA_absx; t[0xB9]=self.LDA_absy
        t[0xA1]=self.LDA_indx;      t[0xB1]=self.LDA_indy

        # LDX
        t[0xA2]=self.LDX_immediate; t[0xA6]=self.LDX_zp;   t[0xB6]=self.LDX_zpy
        t[0xAE]=self.LDX_abs;       t[0xBE]=self.LDX_absy

        # LDY
        t[0xA0]=self.LDY_immediate; t[0xA4]=self.LDY_zp;   t[0xB4]=self.LDY_zpx
        t[0xAC]=self.LDY_abs;       t[0xBC]=self.LDY_absx

        # STA
        t[0x85]=self.STA_zp;  t[0x95]=self.STA_zpx
        t[0x8D]=self.STA_abs; t[0x9D]=self.STA_absx; t[0x99]=self.STA_absy
        t[0x81]=self.STA_indx;t[0x91]=self.STA_indy

        # STX
        t[0x86]=self.STX_zp;  t[0x96]=self.STX_zpy; t[0x8E]=self.STX_abs

        # STY
        t[0x84]=self.STY_zp;  t[0x94]=self.STY_zpx; t[0x8C]=self.STY_abs

        # ADC
        t[0x69]=self.ADC_immediate; t[0x65]=self.ADC_zp;   t[0x75]=self.ADC_zpx
        t[0x6D]=self.ADC_abs;       t[0x7D]=self.ADC_absx; t[0x79]=self.ADC_absy
        t[0x61]=self.ADC_indx;      t[0x71]=self.ADC_indy

        # SBC
        t[0xE9]=self.SBC_immediate; t[0xE5]=self.SBC_zp;   t[0xF5]=self.SBC_zpx
        t[0xED]=self.SBC_abs;       t[0xFD]=self.SBC_absx; t[0xF9]=self.SBC_absy
        t[0xE1]=self.SBC_indx;      t[0xF1]=self.SBC_indy

        # AND
        t[0x29]=self.AND_immediate; t[0x25]=self.AND_zp;   t[0x35]=self.AND_zpx
        t[0x2D]=self.AND_abs;       t[0x3D]=self.AND_absx; t[0x39]=self.AND_absy
        t[0x21]=self.AND_indx;      t[0x31]=self.AND_indy

        # ORA
        t[0x09]=self.ORA_immediate; t[0x05]=self.ORA_zp;   t[0x15]=self.ORA_zpx
        t[0x0D]=self.ORA_abs;       t[0x1D]=self.ORA_absx; t[0x19]=self.ORA_absy
        t[0x01]=self.ORA_indx;      t[0x11]=self.ORA_indy

        # EOR
        t[0x49]=self.EOR_immediate; t[0x45]=self.EOR_zp;   t[0x55]=self.EOR_zpx
        t[0x4D]=self.EOR_abs;       t[0x5D]=self.EOR_absx; t[0x59]=self.EOR_absy
        t[0x41]=self.EOR_indx;      t[0x51]=self.EOR_indy

        # CMP
        t[0xC9]=self.CMP_immediate; t[0xC5]=self.CMP_zp;   t[0xD5]=self.CMP_zpx
        t[0xCD]=self.CMP_abs;       t[0xDD]=self.CMP_absx; t[0xD9]=self.CMP_absy
        t[0xC1]=self.CMP_indx;      t[0xD1]=self.CMP_indy

        # CPX / CPY
        t[0xE0]=self.CPX_immediate; t[0xE4]=self.CPX_zp; t[0xEC]=self.CPX_abs
        t[0xC0]=self.CPY_immediate; t[0xC4]=self.CPY_zp; t[0xCC]=self.CPY_abs

        # INC / DEC
        t[0xE6]=self.INC_zp;  t[0xF6]=self.INC_zpx; t[0xEE]=self.INC_abs; t[0xFE]=self.INC_absx
        t[0xC6]=self.DEC_zp;  t[0xD6]=self.DEC_zpx; t[0xCE]=self.DEC_abs; t[0xDE]=self.DEC_absx

        # Transfers / INX / INY / DEX / DEY
        t[0xAA]=self.TAX; t[0x8A]=self.TXA
        t[0xA8]=self.TAY; t[0x98]=self.TYA
        t[0x9A]=self.TXS; t[0xBA]=self.TSX
        t[0xE8]=self.INX; t[0xC8]=self.INY
        t[0xCA]=self.DEX; t[0x88]=self.DEY

        # ASL
        t[0x0A]=self.ASL_acc; t[0x06]=self.ASL_zp;   t[0x16]=self.ASL_zpx
        t[0x0E]=self.ASL_abs; t[0x1E]=self.ASL_absx

        # LSR
        t[0x4A]=self.LSR_acc; t[0x46]=self.LSR_zp;   t[0x56]=self.LSR_zpx
        t[0x4E]=self.LSR_abs; t[0x5E]=self.LSR_absx

        # ROL
        t[0x2A]=self.ROL_acc; t[0x26]=self.ROL_zp;   t[0x36]=self.ROL_zpx
        t[0x2E]=self.ROL_abs; t[0x3E]=self.ROL_absx

        # ROR
        t[0x6A]=self.ROR_acc; t[0x66]=self.ROR_zp;   t[0x76]=self.ROR_zpx
        t[0x6E]=self.ROR_abs; t[0x7E]=self.ROR_absx

        # BIT
        t[0x24]=self.BIT_zp; t[0x2C]=self.BIT_abs

        # JMP / JSR / RTS / RTI
        t[0x4C]=self.JMP_abs; t[0x6C]=self.JMP_ind
        t[0x20]=self.JSR
        t[0x60]=self.RTS
        t[0x40]=self.RTI

        # Stack
        t[0x48]=self.PHA; t[0x68]=self.PLA
        t[0x08]=self.PHP; t[0x28]=self.PLP

        # Branches
        t[0xF0]=self.BEQ; t[0xD0]=self.BNE
        t[0x90]=self.BCC; t[0xB0]=self.BCS
        t[0x30]=self.BMI; t[0x10]=self.BPL
        t[0x50]=self.BVC; t[0x70]=self.BVS

        # Flags
        t[0x18]=self.CLC; t[0x38]=self.SEC
        t[0x58]=self.CLI; t[0x78]=self.SEI
        t[0xB8]=self.CLV; t[0xD8]=self.CLD; t[0xF8]=self.SED

        # NOP
        t[0xEA]=self.NOP
        return t

    # ------------------------------------------------------------------
    # Execute / step / run
    # ------------------------------------------------------------------
    def execute(self, opcode):
        handler = self._dispatch[opcode]
        if handler is None:
            print(f"  [WARN] Unknown opcode 0x{opcode:02X} at PC=0x{(self.PC-1)&0xFFFF:04X}")
            return
        handler()
        if opcode == 0x00:          # BRK halts execution
            self.running = False

    def step(self):
        opcode = self.fetch_byte()
        self.execute(opcode)
        self.cycles += 1

    def run(self):
        self._dispatch = self._build_dispatch()
        while self.running:
            if self.paused:
                time.sleep(0.05)
                continue
            self.step()

    def reset(self):
        """Perform a hardware reset: load PC from reset vector $FFFC/$FFFD."""
        lo = self.read8(0xFFFC)
        hi = self.read8(0xFFFD)
        self.PC     = lo | (hi << 8)
        self.SP     = 0xFD
        self.STATUS = self.FLAG_UNUSED
        self.running = True

    # ------------------------------------------------------------------
    # Loader helpers
    # ------------------------------------------------------------------
    def load_nasm_file(self, path, pc_override=None):
        """Load and assemble a NASM source file into memory. Sets PC."""
        loader = NASMLoader()
        start  = loader.load_file(path, self.memory, pc_override)
        self.PC = start
        print(f"Loaded {path!r} -> PC set to 0x{start:04X}")

    def load_nasm_string(self, source, pc_override=None):
        """Load and assemble a NASM source string into memory. Sets PC."""
        loader = NASMLoader()
        start  = loader.load_string(source, self.memory, pc_override)
        self.PC = start
        print(f"Loaded inline NASM -> PC set to 0x{start:04X}")

    def load_raw(self, data, start_addr=0x0000):
        """Load raw bytes into memory starting at start_addr."""
        for i, b in enumerate(data):
            self.memory[(start_addr + i) & 0xFFFF] = b & 0xFF
        self.PC = start_addr

    # ------------------------------------------------------------------
    # Dump helpers
    # ------------------------------------------------------------------
    def regdump(self):
        print(f"""
    Register dump:
    Reg A: {self.A:#04x}
    Reg X: {self.X:#04x}
    Reg Y: {self.Y:#04x}""")

    def debugdump(self):
        flags = (
            f"{'N' if self.get_flag(self.FLAG_NEGATIVE)  else 'n'}"
            f"{'V' if self.get_flag(self.FLAG_OVERFLOW)  else 'v'}"
            f"-"
            f"{'B' if self.get_flag(self.FLAG_BREAK)     else 'b'}"
            f"{'D' if self.get_flag(self.FLAG_DECIMAL)   else 'd'}"
            f"{'I' if self.get_flag(self.FLAG_INTERRUPT) else 'i'}"
            f"{'Z' if self.get_flag(self.FLAG_ZERO)      else 'z'}"
            f"{'C' if self.get_flag(self.FLAG_CARRY)     else 'c'}"
        )
        print(f"""
    Debug dump:
    A:  {self.A:#04x}   X:  {self.X:#04x}   Y:  {self.Y:#04x}
    SP: {self.SP:#04x}  PC: {self.PC:#06x}
    STATUS: {self.STATUS:#04x}  [{flags}]
    Cycles: {self.cycles}""")

    def memdump(self, start, length=64):
        """Hex dump of memory."""
        print(f"  Memory dump from 0x{start:04X}:")
        for row in range(0, length, 16):
            addr = start + row
            hex_part  = ' '.join(f'{self.memory[(addr+i)&0xFFFF]:02X}' for i in range(16))
            ascii_part = ''.join(
                chr(self.memory[(addr+i)&0xFFFF]) if 0x20 <= self.memory[(addr+i)&0xFFFF] < 0x7F else '.'
                for i in range(16)
            )
            print(f"  {addr:04X}:  {hex_part}  |{ascii_part}|")



# Demo / self-test

if __name__ == "__main__":
    cpu = emu6502()

    asm_file = "asm.txt"

    if os.path.exists(asm_file):
        try:
            print(f"Loading {asm_file}...\n")
            cpu.load_nasm_file(asm_file)
            cpu.run()
            cpu.debugdump()
        except Exception as e:
            print(f"Failed to run {asm_file}: {e}")
    else:
        print("asm.txt not found, running built-in demo...\n")

        # ---- Example: inline NASM – print "Hello, 6502!\n" via UART ----
        hello_program = """
        ORG $0200

        UART_DATA   EQU $6000

        start:
            LDX #0
        loop:
            LDA message,X
            BEQ done
            STA UART_DATA
            INX
            JMP loop
        done:
            BRK

        message:
            DB "Hello, 6502!", $0D, $0A, 0
        """

        cpu.load_nasm_string(hello_program)
        cpu.run()
        cpu.debugdump()

    print("\n--- Example 2: raw bytes (original style) ---")
    cpu2 = emu6502()
    cpu2.memory[0x0000] = 0xA9  # LDA #5
    cpu2.memory[0x0001] = 0x05
    cpu2.memory[0x0002] = 0xAA  # TAX
    cpu2.memory[0x0003] = 0xE8  # INX
    cpu2.memory[0x0004] = 0x00  # BRK
    cpu2._dispatch = cpu2._build_dispatch()
    cpu2.run()
    cpu2.debugdump()

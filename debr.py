import json
import queue
import warnings

from capstone import *
from elftools.elf.elffile import *
from keystone import *
from unicorn import *
from unicorn.arm64_const import *

CS = Cs(CS_ARCH_ARM64, CS_MODE_ARM)
CS.detail = True

KS = keystone.Ks(keystone.KS_ARCH_ARM64, keystone.KS_MODE_LITTLE_ENDIAN)


def align_addr(addr):
    return addr // 1024 * 1024


def align_size(size):
    return (size + 0x1000) & ~0xfff


def print_regs(uc):
    for i in range(UC_ARM64_REG_X0, UC_ARM64_REG_X28):
        print(f"x{i - UC_ARM64_REG_X0}: {hex(uc.reg_read(i))}")


def set_context(uc, regs):
    if regs is None:
        return
    for i in range(29):  # x0 ~ x28
        idx = UC_ARM64_REG_X0 + i
        uc.reg_write(idx, regs[i])
        uc.reg_write(UC_ARM64_REG_FP, regs[29])  # fp
        uc.reg_write(UC_ARM64_REG_LR, regs[30])  # lr
        uc.reg_write(UC_ARM64_REG_SP, regs[31])  # sp


def get_context(uc):
    regs = []
    for i in range(29):
        idx = UC_ARM64_REG_X0 + i
        regs.append(uc.reg_read(idx))
    regs.append(uc.reg_read(UC_ARM64_REG_FP))
    regs.append(uc.reg_read(UC_ARM64_REG_LR))
    regs.append(uc.reg_read(UC_ARM64_REG_SP))
    return regs


class DeBr:

    def __init__(self, name):
        self.first = None
        self.emu = None
        self.temp_emu = None
        self.file = open(name, "rb")
        self.buf = self.file.read()
        self.elf = ELFFile(self.file)
        self.base = 0x0000000
        self.pc = 0
        self.traced = {}
        self.q = queue.Queue()
        self.ins_stack = []
        self.ins_entry = []
        self.jump_table = {}

    def save(self, name):
        with open(name, "wb") as f:
            f.write(self.buf)

    def load_segment(self):
        start, end = 0xffffffff, 0
        for i in range(0, self.elf.num_segments()):
            seg = self.elf.get_segment(i)
            if seg.header["p_type"] == 'PT_LOAD':
                v_addr = align_addr(seg.header["p_vaddr"])
                v_size = align_size(seg.header["p_memsz"])
                if start > v_addr:
                    start = v_addr
                if v_addr + v_size > end:
                    end = v_addr + v_size

        self.emu.mem_map(self.base + start, end - start)
        self.temp_emu.mem_map(self.base + start, end - start)

        for seg in self.elf.iter_segments("PT_LOAD"):
            f_offset = seg.header["p_offset"]
            f_size = seg.header["p_filesz"]
            v_addr = seg.header["p_vaddr"]
            v_size = seg.header["p_memsz"]

            self.emu.mem_write(self.base + v_addr, self.buf[f_offset:f_offset + f_size])
            self.temp_emu.mem_write(self.base + v_addr, self.buf[f_offset:f_offset + f_size])

    def virtual_to_fileoffset(self, v_addr):
        v_addr = v_addr - self.base
        for seg in self.elf.iter_segments("PT_LOAD"):
            v_start = seg.header["p_vaddr"]
            v_end = v_start + seg.header["p_memsz"]
            f_start = seg.header["p_offset"]
            if v_start <= v_addr < v_end:
                return v_addr - v_start + f_start
        return None

    def patch_bytes(self, old_bytes, new_bytes, addr, length):
        tmp_bytes = old_bytes[:addr] + bytes(new_bytes) + old_bytes[addr + length:]
        return tmp_bytes

    def patch_branch(self, uc, addr, branch):
        if len(branch) == 3:
            self.jump_table[addr] = [
                branch[0],
                branch[1],
                branch[2].op_str.split(', ')[-1]
            ]
        elif len(branch) == 1:
            self.jump_table[addr] = [
                branch[0]
            ]

        if len(branch) == 1:
            asm = 'b' + ' ' + hex(branch[0])
            data1 = KS.asm(asm, addr)[0]
            self.buf = self.patch_bytes(self.buf, data1, self.virtual_to_fileoffset(addr), 4)
        else:
            offset1 = branch[0]
            offset2 = branch[1]
            cond = branch[2]
            # 'x9, x28, x23, lt'
            condstr = cond.op_str.split(', ')[-1]

            asm = 'b' + condstr + ' ' + hex(offset1)
            data1 = KS.asm(asm, cond.address)[0]

            asm1 = 'b' + ' ' + hex(offset2)
            data2 = KS.asm(asm1, addr)[0]

            self.buf = self.patch_bytes(self.buf, data1, self.virtual_to_fileoffset(cond.address), 4)
            self.buf = self.patch_bytes(self.buf, data2, self.virtual_to_fileoffset(addr), 4)

    def emulate_execution(self, start_addr, end_addr):
        self.emu = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        self.temp_emu = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        self.load_segment()
        stack_address = 0xf0000000
        stack_size = 0x100000
        self.emu.mem_map(stack_address, stack_size)
        self.temp_emu.mem_map(stack_address, stack_size)
        self.emu.reg_write(UC_ARM64_REG_SP, stack_address + int(stack_size / 2))
        self.temp_emu.reg_write(UC_ARM64_REG_SP, stack_address + int(stack_size / 2))

        self.first = True

        def hook_code1(uc: Uc, address, size, user_data):
            data = uc.mem_read(address, size)
            ins = list(CS.disasm(data, address))[0]

            if 'bl' in ins.mnemonic:
                uc.reg_write(UC_ARM64_REG_PC, uc.reg_read(UC_ARM64_REG_PC) + 4)
            # print(f"0x{address:x}: {ins.mnemonic} {ins.op_str}")

        def hook_code(uc: Uc, address, size, user_data):
            data = uc.mem_read(address, size)
            ins = list(CS.disasm(data, address))[0]

            if self.first:
                if 'bl' not in ins.mnemonic and 'b' in ins.mnemonic:
                    self.first = False
                    self.ins_entry = [i for i in self.ins_stack[::-1]]

            self.ins_stack.append((address, get_context(uc), ins))

            if 'ret' in ins.mnemonic:
                uc.emu_stop()

            if 'b.' in ins.mnemonic:
                ctx = get_context(uc)
                self.q.put((ins.address + 4, ctx))
                self.q.put((ins.operands[0].imm, ctx))
                uc.emu_stop()

            if 'udf' in ins.mnemonic:
                self.ins_stack.clear()
                uc.emu_stop()
            if 'bl' in ins.mnemonic:
                self.pc = uc.reg_read(UC_ARM64_REG_PC) + 4
                uc.reg_write(UC_ARM64_REG_PC, self.pc)

            if 'br' == ins.mnemonic:
                block_start = self.ins_stack[0][0]
                block_end = self.ins_stack[-1][0]

                def get_double_branch(uc: Uc, ins_stack):
                    jump_regs = None
                    for tup in ins_stack[::-1]:
                        ins = tup[2]
                        ctx = tup[1]
                        if ins.address == 0x7c730:
                            pass

                        if 'br' in ins.mnemonic:
                            jump_regs = ins.operands[0].reg - 218

                        if 'cs' in ins.mnemonic:
                            if ins.address == 0xE5430:
                                pass
                            org = get_context(self.temp_emu)
                            if 'csel' in ins.mnemonic:
                                # CSEL            X9, X28, X23, LT
                                arr = ins.op_str.split(", ")
                                if arr[0][0] in ['x', 'w']:
                                    dest = int(arr[0][1:])

                                if 'xzr' in arr[1] or 'wzr' in arr[1]:
                                    src1v = 0
                                elif arr[1][0] in ['x', 'w']:
                                    src1 = int(arr[1][1:])
                                    src1v = ctx[src1]
                                else:
                                    print("------------------------")

                                if 'xzr' in arr[2] or 'wzr' in arr[2]:
                                    src2v = 0
                                elif arr[2][0] in ['x', 'w']:
                                    src2 = int(arr[2][1:])
                                    src2v = ctx[src2]
                                else:
                                    print("------------------------")
                            elif 'cset' in ins.mnemonic:
                                arr = ins.op_str.split(", ")
                                if 'x' in arr[0] or 'w' in arr[0]:
                                    dest = int(arr[0][1:])
                                    src1v = 0
                                    src2v = 1
                            elif 'csinc' in ins.mnemonic:
                                # CSINC            X9, X28, X23, LT
                                arr = ins.op_str.split(", ")
                                if arr[0][0] in ['x', 'w']:
                                    dest = int(arr[0][1:])

                                if 'xzr' in arr[1] or 'wzr' in arr[1]:
                                    src1v = 0
                                elif arr[1][0] in ['x', 'w']:
                                    src1 = int(arr[1][1:])
                                    src1v = ctx[src1]
                                else:
                                    print("------------------------")

                                if 'xzr' in arr[2] or 'wzr' in arr[2]:
                                    src2v = 0 + 1
                                elif arr[2][0] in ['x', 'w']:
                                    src2 = int(arr[2][1:])
                                    src2v = ctx[src2] + 1
                                else:
                                    print("------------------------")

                            start_ = tup[0] + 4
                            end = ins_stack[-1][0]

                            set_context(self.temp_emu, ctx)
                            self.temp_emu.reg_write(UC_ARM64_REG_X0 + dest, src1v)

                            try:
                                self.temp_emu.emu_start(start_, end)
                            except:
                                pass
                            b1 = self.temp_emu.reg_read(UC_ARM64_REG_X0 + jump_regs)

                            set_context(self.temp_emu, ctx)
                            self.temp_emu.reg_write(UC_ARM64_REG_X0 + dest, src2v)
                            try:
                                self.temp_emu.emu_start(start_, end)
                            except:
                                pass
                            b2 = self.temp_emu.reg_read(UC_ARM64_REG_X0 + jump_regs)

                            set_context(self.temp_emu, org)

                            if b1 != b2:
                                return b1, b2, ins

                ret = None
                try:
                    ret = get_double_branch(uc, self.ins_stack)
                except Exception as e:
                    print(e)
                ctx = get_context(uc)
                if ret is None:
                    print(f"analysis failed: {hex(ins.address)}")
                else:
                    self.q.put((ret[0], ctx))
                    self.q.put((ret[1], ctx))
                    self.patch_branch(uc, block_end, ret)
                    print(f"{block_start:x} Double Branch: {ret[0]:x}, {ret[1]:x}")
                self.ins_stack.clear()
                uc.emu_stop()

        self.pc = self.base + start_addr
        self.emu.reg_write(UC_ARM64_REG_LR, 0x90000000)
        self.emu.hook_add(UC_HOOK_CODE, hook_code)
        self.temp_emu.hook_add(UC_HOOK_CODE, hook_code1)
        self.q.put((start_addr, None))
        while not self.q.empty():
            addr, context = self.q.get()
            if addr in self.traced:
                continue

            self.traced[addr] = 1
            set_context(self.emu, context)
            self.pc = addr
            while True:
                try:
                    self.emu.emu_start(self.pc, 0x90000000)
                    break
                except Exception as e:
                    if not start_addr <= self.pc <= end_addr:
                        warnings.warn(f"pc out of range: {hex(self.pc)}")
                        print_regs(self.emu)
                        break
                    self.pc = self.emu.reg_read(UC_ARM64_REG_PC) + 4

        with open("jump_table.json", 'w+') as f:
            f.write(json.dumps(self.jump_table))

    def get_csel_cset(self, start, end):
        res = []
        code = self.buf[start:end]
        codes = [ins for ins in CS.disasm(code, start)]
        for ins in codes:
            if ins.mnemonic == 'csel' or ins.mnemonic == 'cset':
                print(ins)

    def get_first_block(self, start, end):
        res = []
        code = self.buf[start:end]
        for ins in CS.disasm(code, start):
            if 'bl' not in ins.mnemonic and 'b' in ins.mnemonic:
                break
            else:
                res.append(ins)

        return res, self.buf[start:len(res) * 4]


import argparse

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("-f", "--file", help="The name of the library.", type=str, required=True)
    parser.add_argument('-s', "--start", help="The start address of the function.", required=True)
    parser.add_argument('-e', "--end", help="The end address of the function.", required=True)
    parser.add_argument('-o', "--output", help="The output file.")

    args = parser.parse_args()
    if args.output is None:
        args.output = args.file

    start = int(args.start, 16)
    end = int(args.end, 16)
    obf = DeBr(args.file)
    obf.emulate_execution(start, end)
    obf.save(args.output)
    print("Done")

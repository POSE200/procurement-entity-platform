"""读取旧Word正文的piece table，以及旧Excel工作表。"""
from __future__ import annotations

import io
import re
import struct


def read_ole(data: bytes) -> tuple[str, str]:
    import olefile

    with olefile.OleFileIO(io.BytesIO(data)) as ole:
        if ole.exists('WordDocument'):
            word = ole.openstream('WordDocument').read()
            flags = struct.unpack_from('<H', word, 10)[0]
            if flags & 0x8100:
                raise ValueError('旧Word文档已加密')
            table = ole.openstream('1Table' if flags & 0x0200 else '0Table').read()
            pos = 32
            csw = struct.unpack_from('<H', word, pos)[0]
            pos += 2 + csw * 2
            cslw = struct.unpack_from('<H', word, pos)[0]
            lw_start = pos + 2
            main_chars = struct.unpack_from('<I', word, lw_start + 12)[0]
            pos = lw_start + cslw * 4
            pair_count = struct.unpack_from('<H', word, pos)[0]
            if pair_count <= 33:
                raise ValueError('旧Word版本缺少正文piece table')
            # MS-DOC FibRgFcLcb97中的第34组偏移和长度为fcClx/lcbClx。
            offset, size = struct.unpack_from('<II', word, pos + 2 + 33 * 8)
            if offset + size > len(table):
                raise ValueError('旧Word正文索引超出文件范围')
            clx = table[offset:offset + size]
            i = 0
            while i < len(clx) and clx[i] == 1:
                i += 3 + struct.unpack_from('<H', clx, i + 1)[0]
            if i + 5 > len(clx) or clx[i] != 2:
                raise ValueError('旧Word正文piece table无效')
            length = struct.unpack_from('<I', clx, i + 1)[0]
            plc = clx[i + 5:i + 5 + length]
            if length < 4 or len(plc) != length or (length - 4) % 12:
                raise ValueError('旧Word正文piece table长度无效')
            count = (length - 4) // 12
            cps = struct.unpack_from('<' + 'I' * (count + 1), plc)
            pieces = []
            for n in range(count):
                start, end = cps[n], min(cps[n + 1], main_chars)
                if end <= start:
                    continue
                fc = struct.unpack_from('<I', plc, 4 * (count + 1) + 8 * n + 2)[0]
                compressed = bool(fc & 0x40000000)
                begin = (fc & 0x3fffffff) // (2 if compressed else 1)
                size = (end - start) * (1 if compressed else 2)
                if begin + size > len(word):
                    raise ValueError('旧Word正文片段超出文件范围')
                pieces.append(word[begin:begin + size].decode('cp1252' if compressed else 'utf-16le', errors='replace'))
            text = ''.join(pieces).replace('\r', '\n').replace('\x07', ' | ')
            return re.sub(r'[\x00-\x08\x0b-\x1f]', ' ', text), 'doc'
        if ole.exists('Workbook') or ole.exists('Book'):
            import xlrd

            book = xlrd.open_workbook(file_contents=data, on_demand=True)
            try:
                lines = []
                for sheet in book.sheets():
                    lines.append(f'[工作表 {sheet.name}]')
                    for index in range(sheet.nrows):
                        cells = [str(value) if value != '' else '' for value in sheet.row_values(index)]
                        if any(cells):
                            lines.append('| ' + ' | '.join(cells) + ' |')
                return '\n'.join(lines), 'xls'
            finally:
                book.release_resources()
    raise ValueError('OLE容器不是Word或Excel文档')

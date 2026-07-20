import io

import ezdxf

from app.ai.cad_ir.adapters.from_dxf import dxf_to_ir


def test_missing_insert_block_does_not_sink_supported_geometry() -> None:
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_line((0, 0), (100, 0))
    insert = msp.new_entity("INSERT", {"name": "MISSING_BLOCK", "insert": (0, 0)})
    assert insert.dxf.name not in doc.blocks
    stream = io.StringIO()
    doc.write(stream)

    ir = dxf_to_ir(stream.getvalue().encode())

    assert len(ir.entities) == 1
    assert ir.entities[0].type == "segment"

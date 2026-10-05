"""Schema drift check reports missing tables/columns, not index noise."""

from sqlalchemy import Column, Index, Integer, MetaData, String, Table, create_engine

from app.db.schema_drift import missing_schema_objects


def _metadata(*, with_note: bool = True, with_extra: bool = False) -> MetaData:
    metadata = MetaData()
    columns = [Column("id", Integer, primary_key=True), Column("name", String(20))]
    if with_note:
        columns.append(Column("note", String(20)))
    Table("runs", metadata, *columns)
    if with_extra:
        Table("extra", metadata, Column("id", Integer, primary_key=True))
    return metadata


def test_reports_missing_table_and_column_only(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    live = _metadata(with_note=False)
    Index("ix_runs_name_only_in_db", live.tables["runs"].c.name)
    live.create_all(engine)

    with engine.connect() as connection:
        assert missing_schema_objects(connection, _metadata(with_extra=True)) == [
            "extra",
            "runs.note",
        ]


def test_matching_schema_reports_nothing(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    _metadata().create_all(engine)

    with engine.connect() as connection:
        assert missing_schema_objects(connection, _metadata()) == []

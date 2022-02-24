from django.db.models.sql import compiler


class SQLCompiler(compiler.SQLCompiler):
    def as_sql(self, with_limits=True, with_col_aliases=False, subquery=False):
        """
        Creates the SQL for this query. Returns the SQL string and list
        of parameters.  
        """
        do_offset = with_limits and (self.query.high_mark is not None or self.query.low_mark)
        if not do_offset:
            sql, params = super(SQLCompiler, self).as_sql(
                with_limits=False,
                with_col_aliases=with_col_aliases,
            )
        else:
            sql, params = super(SQLCompiler, self).as_sql(
                with_limits=False,
                with_col_aliases=True,
            )
            high_where = ''
            if self.query.high_mark is not None:
                high_where = 'WHERE ROWNUM <= %d' % (self.query.high_mark,)
            sql = (
                'SELECT * FROM (SELECT "_SUB".*, ROWNUM AS "_RN" FROM (%s) '
                '"_SUB" %s) WHERE "_RN" > %d' % (sql, high_where, self.query.low_mark)
            )

        return sql, params


class SQLInsertCompiler(compiler.SQLInsertCompiler, SQLCompiler):
    pass


class SQLDeleteCompiler(compiler.SQLDeleteCompiler, SQLCompiler):
    pass


class SQLUpdateCompiler(compiler.SQLUpdateCompiler, SQLCompiler):
    pass


class SQLAggregateCompiler(compiler.SQLAggregateCompiler, SQLCompiler):
    pass

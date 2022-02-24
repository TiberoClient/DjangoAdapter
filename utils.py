import datetime

from django.utils.encoding import force_bytes, force_text

from .base import Database

# Set convert_unicode 
convert_unicode = force_bytes

class InsertIdVar(object):
    """
    A late-binding cursor variable that can be passed to Cursor.execute
    as a parameter, in order to receive the id of the row created by an
    insert statement.
    """

    def bind_parameter(self, cursor):
        param = cursor.cursor.var(Database.NUMBER)
        cursor._insert_id_var = param
        return param


class Tibero_datetime(datetime.datetime):
    """
    A datetime object with additional microseconds.
    """

    @classmethod
    def from_datetime(cls, dt):
        return Tibero_datetime(
            dt.year, dt.month, dt.day,
            dt.hour, dt.minute, dt.second, dt.microsecond,
        )

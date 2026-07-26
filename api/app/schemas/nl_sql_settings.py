from pydantic import BaseModel


# ══════════════════════════════════════════════════════════════════════════════
# NL-SQL chat reply verbosity (single row)
# ══════════════════════════════════════════════════════════════════════════════

class NlSqlChatSettingsOut(BaseModel):
    show_technical_details: bool = False


class NlSqlChatSettingsUpdate(BaseModel):
    show_technical_details: bool

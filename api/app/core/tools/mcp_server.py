import os
import sys
from sqlalchemy import text
from mcp.server.fastmcp import FastMCP

# Ensure the api/ directory (the parent of the `app` package) is on the path so
# `import app...` resolves. This file lives at app/core/tools/, so walk up 4 levels.
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from app.core import database
from app import models
from app.core.rag import rag_service

# Initialize FastMCP Server
mcp = FastMCP("Oracle EBS AI Agent Server")

@mcp.tool()
def ebs_query_database(sql_query: str) -> str:
    """
    Executes a read-only SQL query against the local Oracle EBS database representation.
    Use this to query tables like users, agent runs, concurrent jobs, etc.
    
    Args:
        sql_query: A standard SQL read-only SELECT statement.
    """
    query_upper = sql_query.upper().strip()
    
    # Restrict write operations for absolute safety
    forbidden = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE", "REPLACE", "GRANT", "REVOKE"]
    if any(f in query_upper for f in forbidden):
        return "ERROR: Writing or mutating database states is strictly prohibited on the MCP gateway."
        
    db = database.SessionLocal()
    try:
        res = db.execute(text(sql_query))
        columns = res.keys()
        rows = res.fetchall()
        
        if not rows:
            return "Query executed successfully. 0 rows returned."
            
        # Format markdown-like table response
        output = [" | ".join(columns)]
        output.append("-" * (len(output[0]) + 4))
        
        # Capped to prevent token overflows
        capped_rows = rows[:50]
        for row in capped_rows:
            output.append(" | ".join(str(val) if val is not None else "NULL" for val in row))
            
        if len(rows) > 50:
            output.append(f"\n... (Truncated. Displaying 50 out of {len(rows)} total rows)")
            
        return "\n".join(output)
    except Exception as e:
        return f"SQL Error during execution: {str(e)}"
    finally:
        db.close()


@mcp.tool()
def ebs_inspect_table(table_name: str) -> str:
    """
    Inspects column structures, types, and constraints for any mock EBS system table.
    Use this before writing SQL queries to prevent column name errors.
    
    Args:
        table_name: Name of the table (e.g. users, chat_messages, agent_runs).
    """
    db = database.SessionLocal()
    try:
        sql = f"""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = '{table_name.lower().strip()}'
        ORDER BY ordinal_position
        """
        res = db.execute(text(sql)).fetchall()
        if not res:
            return f"Table '{table_name}' not found or is empty."
            
        output = [f"Columns schema for table: {table_name}"]
        output.append("Column Name | Data Type | Nullable")
        output.append("-" * 40)
        for row in res:
            output.append(f"{row[0]} | {row[1]} | {row[2]}")
        return "\n".join(output)
    except Exception as e:
        return f"Schema inspection failed: {str(e)}"
    finally:
        db.close()


@mcp.tool()
def ebs_check_concurrent_jobs(status_filter: str = None) -> str:
    """
    Inspects the status of active concurrent program jobs and diagnostic agent runs.
    
    Args:
        status_filter: Optional filter ('running', 'completed', 'failed').
    """
    db = database.SessionLocal()
    try:
        query = db.query(models.AgentRun)
        if status_filter:
            query = query.filter(models.AgentRun.status == status_filter.lower().strip())
            
        runs = query.order_by(models.AgentRun.created_at.desc()).limit(20).all()
        if not runs:
            return "No concurrent agent runs found matching filter."
            
        output = ["Concurrent Diagnostic Agent Jobs:"]
        output.append("Run ID | Agent ID | Status | Created At")
        output.append("-" * 55)
        for r in runs:
            output.append(f"{r.id} | {r.agent_id} | {r.status} | {r.created_at}")
        return "\n".join(output)
    except Exception as e:
        return f"Failed to check concurrent jobs: {str(e)}"
    finally:
        db.close()


@mcp.tool()
def ebs_query_knowledge_base(query: str) -> str:
    """
    Searches local vector knowledge bases (PDFs, manual uploads) to resolve
    troubleshooting, optimization, or setup scripts.
    
    Args:
        query: Troubleshooting search phrase or parameter.
    """
    try:
        context = rag_service.query_rag(query)
        if not context:
            return "No relevant documentation matches found inside the local RAG knowledge base."
        return context
    except Exception as e:
        return f"RAG Query error: {str(e)}"


@mcp.tool()
def ebs_web_search(query: str) -> str:
    """
    Queries live internet engines for official Oracle EBS setup documentation
    when local references are missing.
    
    Args:
        query: Live search keywords.
    """
    try:
        results = rag_service.search_web_fallback(query)
        if not results:
            return "No live search results returned from internet search."
        return results
    except Exception as e:
        return f"Web search failed: {str(e)}"


if __name__ == "__main__":
    # Start the FastMCP server over standard I/O (STDIO) transport
    print("[MCP] Launching Oracle EBS Model Context Protocol Server...", file=sys.stderr)
    mcp.run("stdio")

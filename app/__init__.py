"""The nopCommerce product-upload automation, as an application.

    app.parser      xlsx -> rows
    app.errors      the error taxonomy every failure is classified against
    app.colour      colour code resolution (map -> heuristic -> LLM)
    app.store       the Supabase run store (runs / rows / steps / events)
    app.automation  the runner main.run() drives
    app.web         upload UI + JSON API
"""

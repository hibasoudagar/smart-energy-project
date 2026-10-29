"""Builds the React front end from app_src.html.

  backend/static/app.html   -> served by Flask at http://localhost:5000 (works offline)
  frontend_src/preview.html -> online preview (libraries from a CDN, demo data)

Run:  python frontend_src/build.py
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "app_src.html"), encoding="utf-8").read()

local = (src.replace("__REACT__", "/static/react.production.min.js")
            .replace("__REACT_DOM__", "/static/react-dom.production.min.js")
            .replace("__BABEL__", "/static/babel.min.js"))
head, body = local.split('<div id="root">', 1)
page = ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n' + head +
        '</head>\n<body>\n<div id="root">' + body + '\n</body>\n</html>\n')
with open(os.path.join(HERE, "..", "backend", "static", "app.html"), "w", encoding="utf-8") as f:
    f.write(page)

preview = (src.replace("__REACT__", "https://cdnjs.cloudflare.com/ajax/libs/react/18.3.1/umd/react.production.min.js")
              .replace("__REACT_DOM__", "https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.3.1/umd/react-dom.production.min.js")
              .replace("__BABEL__", "https://cdn.jsdelivr.net/npm/@babel/standalone@7.24.7/babel.min.js")
              .replace('<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n', ""))
with open(os.path.join(HERE, "preview.html"), "w", encoding="utf-8") as f:
    f.write(preview)
print("Built backend/static/app.html and frontend_src/preview.html")

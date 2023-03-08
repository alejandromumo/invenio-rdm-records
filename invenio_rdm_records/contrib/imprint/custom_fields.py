# -*- coding: utf-8 -*-
#
# Copyright (C) 2023 CERN.
#
# Invenio-RDM-Records is free software; you can redistribute it and/or modify
# it under the terms of the MIT License; see LICENSE file for more details.
"""Imprint specific custom fields.

Implements the following fields:
- imprint.isbn
- imprint.pages
- imprint.place
- imprint.publisher
- imprint.title
"""

from invenio_i18n import lazy_gettext as _
from invenio_records_resources.services.custom_fields import BaseCF
from marshmallow import fields
from marshmallow_utils.fields import SanitizedUnicode


class ImprintCF(BaseCF):
    """Nested custom field."""

    @property
    def field(self):
        """Imprint fields definitions."""
        return fields.Nested(
            {
                "title": SanitizedUnicode(),
                "isbn": SanitizedUnicode(),
                "publisher": SanitizedUnicode(),
                "pages": SanitizedUnicode(),
                "year": SanitizedUnicode(),
                "place": SanitizedUnicode(),
            }
        )

    @property
    def mapping(self):
        """Imprint search mappings."""
        return {
            "type": "object",
            "properties": {
                "title": {
                    "type": "text",
                    "fields": {"keyword": {"type": "keyword"}},
                },
                "isbn": {"type": "keyword"},
                "publisher": {"type": "keyword"},
                "pages": {"type": "keyword"},
                "place": {"type": "keyword"},
            },
        }


IMPRINT_NAMESPACE = {
    # Imprint
    "imprint": "",
}


IMPRINT_CUSTOM_FIELDS = [ImprintCF(name="imprint:imprint")]

IMPRINT_CUSTOM_FIELDS_UI = {
    "section": _("Book / Report / Chapter"),
    "fields": [
        {
            "field": "imprint:imprint",
            "ui_widget": "Imprint",
            "template": "imprint.html",
            "props": {
                "label": _("Imprint"),
                "publisher": {
                    "label": _("Publisher"),
                    "placeholder": _(""),
                    "description": _("Book's publisher"),
                },
                "place": {
                    "label": _("Place"),
                    "placeholder": _("e.g. city, country"),
                    "description": _("Place where the book was published"),
                },
                "isbn": {
                    "label": _("ISBN"),
                    "placeholder": _("e.g. 0-06-251587-X"),
                    "description": _("International Standard Book Number (ISBN)"),
                },
                "title": {
                    "label": _("Book title"),
                    "placeholder": _("Add the book title..."),
                    "description": _(
                        "Title of the book or report which this upload is part of."
                    ),
                },
                "pages": {
                    "label": _("Pages"),
                    "placeholder": _(""),
                    "description": _("Book pages on which this record was published"),
                },
                "icon": "lab",
                "description": "For parts of books (e.g. chapters) and reports.",
            },
        }
    ],
}

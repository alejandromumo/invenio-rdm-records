# -*- coding: utf-8 -*-
#
# Copyright (C) 2020-2021 CERN.
# Copyright (C) 2020-2021 Northwestern University.
# Copyright (C) 2021 TU Wien.
# Copyright (C) 2023 Graz University of Technology.
#
# Invenio-RDM-Records is free software; you can redistribute it and/or modify
# it under the terms of the MIT License; see LICENSE file for more details.

"""RDM record access settings service."""

from datetime import datetime, timedelta

import arrow
from flask import current_app, url_for
from invenio_access.permissions import authenticated_user, system_identity
from invenio_drafts_resources.services.records import RecordService
from invenio_i18n import lazy_gettext as _
from invenio_records_resources.services.errors import PermissionDeniedError
from invenio_records_resources.services.records.schema import ServiceSchemaWrapper
from invenio_records_resources.services.uow import RecordCommitOp, unit_of_work
from invenio_requests.proxies import current_requests_service
from invenio_users_resources.proxies import current_user_resources
from marshmallow.exceptions import ValidationError
from sqlalchemy.orm.exc import NoResultFound

from ...requests.access import AccessRequestToken, GuestAccessRequest, UserAccessRequest
from ...requests.access.requests import EmailOp
from ...secret_links.errors import InvalidPermissionLevelError
from ..errors import DuplicateAccessRequestError
from ..results import GrantSubjectExpandableField


class RecordAccessService(RecordService):
    """RDM Secret Link service."""

    def link_result_item(self, *args, **kwargs):
        """Create a new instance of the resource unit."""
        return self.config.link_result_item_cls(*args, **kwargs)

    def link_result_list(self, *args, **kwargs):
        """Create a new instance of the resource list."""
        return self.config.link_result_list_cls(*args, **kwargs)

    def grant_result_item(self, *args, **kwargs):
        """Create a new instance of the resource unit."""
        kwargs["expandable_fields"] = self.expandable_fields
        return self.config.grant_result_item_cls(*args, **kwargs)

    def grant_result_list(self, *args, **kwargs):
        """Create a new instance of the resource list."""
        kwargs["expandable_fields"] = self.expandable_fields
        return self.config.grant_result_list_cls(*args, **kwargs)

    def get_parent_and_record_or_draft(self, _id):
        """Return parent and (record or draft)."""
        try:
            record, parent = self._get_record_and_parent_by_id(_id)
        except NoResultFound:
            record, parent = self._get_draft_and_parent_by_id(_id)
        return record, parent

    @property
    def schema_secret_link(self):
        """Schema for secret links."""
        return ServiceSchemaWrapper(self, schema=self.config.schema_secret_link)

    @property
    def schema_grant(self):
        """Schema for secret links."""
        return ServiceSchemaWrapper(self, schema=self.config.schema_grant)

    @property
    def expandable_fields(self):
        """List of expandable fields."""
        return [
            GrantSubjectExpandableField("subject"),
        ]

    #
    # Secret links
    #

    def _validate_secret_link_expires_at(
        self, expires_at, is_specified=True, secret_link=None
    ):
        """Validate the given expiration date.

        If a ``secret_link`` is specified, the validity of setting its
        expiration date to ``expires_at`` will be checked additionally.
        The ``is_specified`` flag hints at if the value of ``expires_at``
        was set in the given data, or if it was omitted (which makes a
        difference in patch operations).
        """
        if expires_at and is_specified:
            # if the expiration date was specified, check if it's in the future
            expires_at = arrow.get(expires_at).to("utc").datetime
            expires_at = expires_at.replace(tzinfo=None)

            if expires_at < datetime.utcnow():
                raise ValidationError(
                    message=_("Expiration date must be set to the future"),
                    field_name="expires_at",
                )

        if secret_link is not None:
            # if we're updating an existing secret link, we need to do some
            # more checks...

            # we interpret explicitly setting 'expires_at = null/None' as
            # removing the expiration date (semantically different from not
            # specifying an 'expires_at' value at all, at least for updates)
            introduces_expiration = (
                is_specified and not expires_at and secret_link.expires_at
            )
            extends_existing_expiration = (
                expires_at
                and secret_link.expires_at
                and expires_at > secret_link.expires_at
            )
            increases_expiration = introduces_expiration or extends_existing_expiration

            if increases_expiration:
                # it's not a problem to reduce the validity of a token (*),
                # but increasing its lifespan would require a new signature,
                # and thus a new token
                # (*) in that case, the permission generator will still say
                #     no, even if the signature is still valid
                raise ValidationError(
                    message=_("Cannot postpone expiration of links"),
                    field_name="expires_at",
                )

            elif expires_at and expires_at < datetime.utcnow():
                raise ValidationError(
                    message=_("Expiration date must be set to the future"),
                    field_name="expires_at",
                )

        return expires_at

    @unit_of_work()
    def create_secret_link(self, identity, id_, data, links_config=None, uow=None):
        """Create a secret link for a record (resp. its parent)."""
        record, parent = self.get_parent_and_record_or_draft(id_)

        # Permissions
        self.require_permission(identity, "manage", record=record)

        # Validation
        data, __ = self.schema_secret_link.load(
            data, context=dict(identity=identity), raise_errors=True
        )
        expires_at = self._validate_secret_link_expires_at(data.get("expires_at"))
        if "permission" not in data:
            raise ValidationError(
                _("An access permission level is required"),
                field_name="permission",
            )

        # Creation
        try:
            link = parent.access.links.create(
                permission_level=data["permission"],
                origin=data.get("origin"),
                description=data.get("description", ""),
                expires_at=expires_at,
                extra_data=data.get("extra_data", {}),
            )
        except InvalidPermissionLevelError:
            raise ValidationError(
                _("Invalid access permission level."),
                field_name="permission",
            )

        # Commit
        uow.register(RecordCommitOp(parent))
        if record:
            uow.register(RecordCommitOp(record))

        # Index all child records of the parent
        self._index_related_records(record, parent, uow=uow)

        return self.link_result_item(
            self,
            identity,
            link,
            links_config=links_config,
        )

    def read_all_secret_links(
        self,
        identity,
        id_,
        links_config=None,
    ):
        """Read the secret links of a record (resp. its parent)."""
        record, parent = self.get_parent_and_record_or_draft(id_)

        # Permissions
        self.require_permission(identity, "manage", record=record)

        # Fetching
        links = parent.access.links.resolve_all()
        return self.link_result_list(
            service=self,
            identity=identity,
            results=links,
            links_config=links_config,
        )

    def read_secret_link(
        self,
        identity,
        id_,
        link_id,
        links_config=None,
    ):
        """Read a specific secret link of a record (resp. its parent)."""
        record, parent = self.get_parent_and_record_or_draft(id_)

        # Permissions
        self.require_permission(identity, "manage", record=record)

        # Fetching
        link_ids = [link.link_id for link in parent.access.links]
        if str(link_id) not in link_ids:
            raise LookupError(str(link_id))

        link_idx = link_ids.index(link_id)
        link = parent.access.links[link_idx].resolve()

        return self.link_result_item(
            self,
            identity,
            link,
            links_config=links_config,
        )

    @unit_of_work()
    def update_secret_link(
        self,
        identity,
        id_,
        link_id,
        data,
        links_config=None,
        uow=None,
    ):
        """Update a secret link for a record (resp. its parent)."""
        record, parent = self.get_parent_and_record_or_draft(id_)

        # Permissions
        self.require_permission(identity, "manage", record=record)

        # Fetching (required for parts of the validation)
        link_ids = [link.link_id for link in parent.access.links]
        if str(link_id) not in link_ids:
            raise LookupError(str(link_id))

        link_idx = link_ids.index(link_id)
        link = parent.access.links[link_idx].resolve()

        # Validation
        data, __ = self.schema_secret_link.load(
            data, context=dict(identity=identity), raise_errors=True
        )
        permission = data.get("permission")
        expires_at = self._validate_secret_link_expires_at(
            data.get("expires_at"),
            is_specified=("expires_at" in data),
            secret_link=link,
        )

        # Update
        # we can't update the link's extra data, as that is encoded
        # in the token and would thus require a new token
        link.expires_at = expires_at or link.expires_at
        link.permission_level = permission or link.permission_level
        link.description = data.get("description", link.description)

        # Commit
        uow.register(RecordCommitOp(parent))
        if record:
            uow.register(RecordCommitOp(record))

        # Index all child records of the parent
        self._index_related_records(record, parent, uow=uow)

        return self.link_result_item(
            self,
            identity,
            link,
            links_config=links_config,
        )

    @unit_of_work()
    def delete_secret_link(self, identity, id_, link_id, links_config=None, uow=None):
        """Delete a secret link for a record (resp. its parent)."""
        record, parent = self.get_parent_and_record_or_draft(id_)

        # Permissions
        self.require_permission(identity, "manage", record=record)

        # Fetching
        link_ids = [link.link_id for link in parent.access.links]
        if str(link_id) not in link_ids:
            raise LookupError(str(link_id))

        link_idx = link_ids.index(link_id)
        link = parent.access.links[link_idx].resolve()

        # Deletion
        parent.access.links.pop(link_idx)
        link.revoke()

        # Commit
        uow.register(RecordCommitOp(parent))
        if record:
            uow.register(RecordCommitOp(record))

        # Index all child records of the parent
        self._index_related_records(record, parent, uow=uow)

        return True

    #
    # Access grants
    #

    def _check_grant_subject(self, identity, grant):
        """Check if the grant subject exists and is visible to the given identity."""
        try:
            if grant.subject_type == "user":
                current_user_resources.users_service.read(
                    identity=identity, id_=grant.subject_id
                )
            elif grant.subject_type == "role":
                current_user_resources.groups_service.read(
                    identity=identity, id_=grant.subject_id
                )
            elif grant.subject_type == "system_role":
                # NOTE: system roles don't have a service yet, so we check through
                #       the system field's resolution (`grant.subject`)
                grant.subject
            else:
                return False

            return True

        except (LookupError, PermissionDeniedError):
            # NOTE: services in Users-Resources will use "permission denied" to mask
            #       "not found" errors, to not leak information about existence
            return False

    @unit_of_work()
    def create_grant(self, identity, id_, data, expand=False, uow=None):
        """Create an access grant for a record (resp. its parent)."""
        record, parent = self.get_parent_and_record_or_draft(id_)

        # Permissions
        self.require_permission(identity, "manage", record=record)

        # Validation
        data, __ = self.schema_grant.load(
            data, context={"identity": identity}, raise_errors=True
        )

        # Creation
        grant = parent.access.grants.create(
            subject_type=data["subject"]["type"],
            subject_id=data["subject"]["id"],
            permission=data["permission"],
            origin=data.get("origin"),
        )

        if not self._check_grant_subject(identity, grant):
            raise ValidationError(
                _("Could not find the specified subject."), field_name="subject.id"
            )

        # Commit
        uow.register(RecordCommitOp(parent))
        if record:
            uow.register(RecordCommitOp(record))

        # Index all child records of the parent
        self._index_related_records(record, parent, uow=uow)

        return self.grant_result_item(
            self,
            identity,
            grant,
            expand=expand,
        )

    def read_grant(self, identity, id_, grant_id, expand=False):
        """Read a specific access grant of a record (resp. its parent)."""
        record, parent = self.get_parent_and_record_or_draft(id_)

        # Permissions
        self.require_permission(identity, "manage", record=record)

        # Fetching
        if not 0 <= grant_id < len(parent.access.grants):
            raise LookupError(str(grant_id))

        grant = parent.access.grants[grant_id]

        return self.grant_result_item(
            self,
            identity,
            grant,
            expand=expand,
        )

    @unit_of_work()
    def update_grant(
        self,
        identity,
        id_,
        grant_id,
        data,
        expand=False,
        partial=False,
        uow=None,
    ):
        """Update an access grant for a record (resp. its parent)."""
        record, parent = self.get_parent_and_record_or_draft(id_)

        # Permissions
        self.require_permission(identity, "manage", record=record)

        # Fetching (required for parts of the validation)
        old_grant = parent.access.grants[grant_id]
        if partial:
            data = {
                "permission": data.get("permission", old_grant.permission),
                "subject": {
                    "type": data.get("subject", {}).get("type", old_grant.subject_type),
                    "id": data.get("subject", {}).get("id", old_grant.subject_id),
                },
                "origin": data.get("origin", old_grant.origin),
            }

        # Validation
        data, __ = self.schema_grant.load(
            data, context={"identity": identity}, raise_errors=True
        )

        # Update
        try:
            new_grant = parent.access.grants.grant_cls.create(
                origin=data["origin"],
                permission=data["permission"],
                subject_type=data["subject"]["type"],
                subject_id=data["subject"]["id"],
                resolve_subject=True,
            )
        except LookupError:
            raise ValidationError(
                _("Could not find the specified subject."), field_name="subject.id"
            )

        parent.access.grants[grant_id] = new_grant

        # Commit
        uow.register(RecordCommitOp(parent))
        if record:
            uow.register(RecordCommitOp(record))

        # Index all child records of the parent
        self._index_related_records(record, parent, uow=uow)

        return self.grant_result_item(
            self,
            identity,
            new_grant,
            expand=expand,
        )

    def read_all_grants(self, identity, id_, expand=False):
        """Read the access grants of a record (resp. its parent)."""
        record, parent = self.get_parent_and_record_or_draft(id_)

        # Permissions
        self.require_permission(identity, "manage", record=record)

        # Fetching
        return self.grant_result_list(
            service=self,
            identity=identity,
            results=parent.access.grants,
            expand=expand,
        )

    @unit_of_work()
    def delete_grant(self, identity, id_, grant_id, uow=None):
        """Delete an access grant for a record (resp. its parent)."""
        record, parent = self.get_parent_and_record_or_draft(id_)

        # Permissions
        self.require_permission(identity, "manage", record=record)

        # Fetching
        if not 0 <= grant_id < len(parent.access.grants):
            raise LookupError(str(grant_id))

        # Deletion
        parent.access.grants.pop(grant_id)

        # Commit
        uow.register(RecordCommitOp(parent))
        if record:
            uow.register(RecordCommitOp(record))

        # Index all child records of the parent
        self._index_related_records(record, parent, uow=uow)

        return True

    #
    # Access requests
    #

    @unit_of_work()
    def create_user_access_request(
        self, identity, id_, message, expand=False, uow=None
    ):
        """Create a user access request for the given record."""
        record = self.record_cls.pid.resolve(id_)

        # Permissions
        self.require_permission(identity, "read", record=record)
        denied = False
        try:
            self.require_permission(identity, "read_files", record=record)
        except PermissionDeniedError:
            denied = True

        if not denied:
            raise PermissionDeniedError()

        # Detect duplicate requests
        req_cls = current_requests_service.record_cls
        model_cls = req_cls.model_cls
        requests = [
            request
            for request in (
                req_cls(rm.data, model=rm)
                for rm in model_cls.query.filter(
                    model_cls.json["created_by"] == {"user": str(identity.id)},
                    model_cls.json["topic"] == {"record": id_},
                )
                if rm.data and rm.data["type"] == UserAccessRequest.type_id
            )
            if request.is_open
        ]

        if requests:
            raise DuplicateAccessRequestError([str(r.id) for r in requests])

        record = self.record_cls.pid.resolve(id_)
        data = {
            "payload": {
                "permission": "view",
                "message": message,
            }
        }

        # Determine the request's receiver
        receiver = None
        record_owner = record.parent.access.owner.resolve()
        if record_owner:
            receiver = record_owner

        if receiver is None:
            pass

        request = current_requests_service.create(
            identity,
            data,
            UserAccessRequest,
            receiver,
            topic=record,
            expires_at=None,
            expand=expand,
            uow=uow,
        )

        # immediately submit the request, unless it has errors
        if request.errors:
            return request

        message = {
            "payload": {
                "content": data["payload"].get("message") or "",
            }
        }

        return current_requests_service.execute_action(
            identity,
            request.id,
            "submit",
            data=message,
            uow=uow,
        )

    @unit_of_work()
    def create_guest_access_request_token(
        self, identity, id_, data, expand=False, uow=None
    ):
        """Create an request token that can be used to create an access request."""
        # Permissions
        if authenticated_user in identity.provides:
            raise PermissionDeniedError("request_guest_access")

        record = self.record_cls.pid.resolve(id_)
        if current_app.config.get("MAIL_SUPPRESS_SEND", False):
            current_app.logger.warn(
                "Cannot proceed with guest based access request - "
                "email sending has been disabled!"
            )

        access_token = AccessRequestToken.create(
            email=data["email"],
            full_name=data["full_name"],
            message=data["message"],
            record_pid=id_,
            shelf_life=timedelta(hours=6),
        )

        # Create the URL for the email verification endpoint
        verify_url = url_for(
            "invenio_rdm_records_ext.verify_access_request_token",
            _external=True,
            **{"access_request_token": access_token.token},
        ).replace("/api/", "/")

        uow.register(
            EmailOp(
                receiver=data["email"],
                subject=_(
                    "Access request for '%(record_title)s'",
                    record_title=record.metadata["title"],
                ),
                html_body=_(
                    (
                        "Please verify your e-mail address via the following link "
                        "in order to submit the access request: "
                        '<a href="%(url)s">%(url)s</a>'
                    ),
                    url=verify_url,
                ),
                body=_(
                    (
                        "Please verify your e-mail address via the following link "
                        "in order to submit the access request: %(url)s"
                    ),
                    url=verify_url,
                ),
            )
        )

        return {
            "message": _("Verification link sent out, please check your e-mail inbox"),
        }

    @unit_of_work()
    def create_guest_access_request(self, identity, token, expand=False, uow=None):
        """Use the guest access request token to create an access request."""
        # Permissions
        if authenticated_user in identity.provides:
            raise PermissionDeniedError("request_guest_access")

        access_token = AccessRequestToken.get_by_token(token)
        if access_token is None:
            return None

        access_token_data = access_token.to_dict()
        record = self.record_cls.pid.resolve(access_token_data["record_pid"])

        # Detect duplicate requests
        req_cls = current_requests_service.record_cls
        model_cls = req_cls.model_cls
        requests = [
            request
            for request in (
                req_cls(rm.data, model=rm)
                for rm in model_cls.query.filter(
                    model_cls.json["created_by"] == {"email": access_token.email},
                    model_cls.json["topic"] == {"record": access_token.record_pid},
                )
                if rm.data and rm.data["type"] == GuestAccessRequest.type_id
            )
            if request.is_open
        ]

        if requests:
            raise DuplicateAccessRequestError([str(r.id) for r in requests])

        data = {
            "payload": {
                "permission": "view",
                "email": access_token_data["email"],
                "full_name": access_token_data["full_name"],
                "token": access_token_data["token"],
                "message": access_token_data.get("message") or "",
            }
        }

        receiver = None
        record_owner = record.parent.access.owner.resolve()
        if record_owner:
            receiver = record_owner

        if receiver is None:
            pass

        access_token.delete()
        request = current_requests_service.create(
            system_identity,
            data,
            GuestAccessRequest,
            receiver,
            creator=data["payload"]["email"],
            topic=record,
            expires_at=None,
            expand=expand,
            uow=uow,
        )

        if request.errors:
            return request

        prefix = _(
            "%(full_name)s (%(email)s) commented",
            full_name=access_token_data["full_name"],
            email=data["payload"]["email"],
        )
        message = data["payload"].get("message") or ""
        comment = {"payload": {"content": f"{prefix}: {message}"}}

        return current_requests_service.execute_action(
            system_identity,
            request.id,
            "submit",
            data=comment,
            uow=uow,
        )

from collections import OrderedDict

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.core.paginator import Paginator
from django.db.models.query import QuerySet
from rest_framework import status, viewsets
from rest_framework.response import Response

from .rpc_mixin import RpcMixin


VALIDATION_ERRORS_KEY = "validation_errors"
ERRORS_KEY = "errors"
OBJ_NOT_FOUND_KEY = "not_found"
OBJ_NOT_FOUND_ERROR_VALUE = "No object found with that ID"


def querydict_to_dict(querydict):
    return {k: v[0] if len(v) == 1 else v for k, v in querydict.lists()}


class RpcDrfMixin(object):
    """
    Provides common DRF ViewSet-like abstractions for interacting with models
    and serializers via RPC.
    """
    queryset = None
    serializer_class = None
    lookup_field = "pk"
    lookup_kwarg = None

    def get_queryset(self):
        assert self.queryset is not None, (
            "'%s' should either include a `queryset` attribute, "
            "or override the `get_queryset()` method."
            % self.__class__.__name__
        )

        queryset = self.queryset
        if isinstance(queryset, QuerySet):
            # Ensure queryset is re-evaluated on each request.
            queryset = queryset.all()
        return queryset

    def get_object(self, **kwargs):
        queryset = self.queryset

        # Perform the lookup filtering.
        lookup_kwarg = self.lookup_kwarg or self.lookup_field

        assert lookup_kwarg in kwargs, (
            'Expected a keyword argument '
            'named "%s". Fix your RPC call, or set the `.lookup_field` '
            'attribute on the service correctly.' %
            (lookup_kwarg,)
        )

        filter_kwargs = {self.lookup_field: kwargs[lookup_kwarg]}

        obj = queryset.get(**filter_kwargs)
        return obj

    def get_serializer_class(self):
        assert self.serializer_class is not None, (
            "'%s' should either include a `serializer_class` attribute, "
            "or override the `get_serializer_class()` method."
            % self.__class__.__name__
        )

        return self.serializer_class

    def get_serializer(self, *args, **kwargs):
        serializer_class = self.get_serializer_class()
        return serializer_class(*args, **kwargs)

    def paginate_queryset(self, queryset, page_num, page_size):
        self.page_num = int(page_num)
        self.page_size = int(page_size)
        paginator = Paginator(queryset, self.page_size)
        self.page = paginator.page(self.page_num)
        return self.page

    def get_paginated_response(self, data):
        return OrderedDict([
            ("results", data),
            ("meta", {
                "total_results": self.page.paginator.count,
                "total_pages": self.page.paginator.num_pages,
                "page": self.page_num,
                "page_size": self.page_size,
            })
        ])

    def create(self, *args, **kwargs):
        serializer = self.get_serializer(data=kwargs)

        if not serializer.is_valid():
            return {VALIDATION_ERRORS_KEY: serializer.errors}

        serializer.save()
        return serializer.data

    def list(self, *args, **kwargs):
        queryset = self.get_queryset()
        page_num = kwargs.pop("page", 1)
        page_size = kwargs.pop("page_size", settings.DYNAMIC_REST["PAGE_SIZE"])
        fields = kwargs.pop("fields", None)
        if fields:
            kwargs["only_fields"] = fields.split(",")

        page = self.paginate_queryset(queryset, page_num, page_size)
        if page is not None:
            serializer = self.get_serializer(page, many=True, *args, **kwargs)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True, *args, **kwargs)
        return serializer.data

    def retrieve(self, *args, **kwargs):
        try:
            instance = self.get_object(**kwargs)
        except ObjectDoesNotExist:
            return {ERRORS_KEY: {OBJ_NOT_FOUND_KEY: OBJ_NOT_FOUND_ERROR_VALUE}}

        serializer = self.get_serializer(instance)
        return serializer.data

    def update(self, *args, **kwargs):
        partial = kwargs.pop("partial", False)

        try:
            instance = self.get_object(**kwargs)
        except ObjectDoesNotExist:
            return {ERRORS_KEY: {OBJ_NOT_FOUND_KEY: OBJ_NOT_FOUND_ERROR_VALUE}}

        serializer = self.get_serializer(instance, data=kwargs, partial=partial)

        if not serializer.is_valid():
            return {VALIDATION_ERRORS_KEY: serializer.errors}

        serializer.save()
        return serializer.data


class RpcDrfViewSet(viewsets.ViewSet, RpcMixin):
    """
    A DRF based ViewSet base class that provides a CRUDL HTTP API gateway
    to interact with Nameko RPC calls.
    """

    rpc_service_name = None

    def get_rpc_service_name(self):
        assert self.rpc_service_name is not None, (
            "'%s' should either include a `rpc_service_name` attribute, "
            "or override the `get_rpc_service_name()` method."
            % self.__class__.__name__
        )

        rpc_service_name = self.rpc_service_name
        return rpc_service_name

    def list(self, request, *args, **kwargs):
        status_code = status.HTTP_200_OK

        params = querydict_to_dict(request.query_params)

        resp = self.call_service_method(
            self.get_rpc_service_name(),
            "list",
            False,
            **params,
        )

        return Response(resp, status=status_code)

    def retrieve(self, request, pk, *args, **kwargs):
        status_code = status.HTTP_200_OK

        params = querydict_to_dict(request.query_params)

        resp = self.call_service_method(
            self.get_rpc_service_name(),
            "retrieve",
            False,
            **{**{"pk": pk}, **params},
        )

        if ERRORS_KEY in resp.keys():
            if OBJ_NOT_FOUND_KEY in resp[ERRORS_KEY]:
                status_code = status.HTTP_404_NOT_FOUND

        return Response(resp, status=status_code)

    def create(self, request, *args, **kwargs):
        status_code = status.HTTP_201_CREATED

        resp = self.call_service_method(
            self.get_rpc_service_name(),
            "create",
            False,
            **request.data
        )

        if VALIDATION_ERRORS_KEY in resp.keys():
            status_code = status.HTTP_400_BAD_REQUEST

        return Response(resp, status=status_code)

    def update(self, request, pk, *args, **kwargs):
        status_code = status.HTTP_200_OK
        request_data = request.data
        request_data["partial"] = kwargs.pop("partial", False)

        resp = self.call_service_method(
            self.get_rpc_service_name(),
            "update",
            False,
            **{**{"pk": pk}, **request_data}
        )

        if ERRORS_KEY in resp.keys():
            if OBJ_NOT_FOUND_KEY in resp[ERRORS_KEY]:
                status_code = status.HTTP_404_NOT_FOUND

        if VALIDATION_ERRORS_KEY in resp.keys():
            status_code = status.HTTP_400_BAD_REQUEST

        return Response(resp, status=status_code)

    def partial_update(self, request, pk, *args, **kwargs):
        kwargs["partial"] = True
        return self.update(request, pk, *args, **kwargs)

from __future__ import annotations

import logging

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.environments.models import Environment
from apps.reports.services import list_all_reports, list_reports, load_report_dataset, load_report_snapshot, query_report_summaries, read_report_content, scan_report_tree
from apps.reports.cpd_data_service import find_cpd_excel_files, preview_cpd_excel



logger = logging.getLogger("tracelens.cpd_report_api")


class CpdReportViewSet(viewsets.ViewSet):
    def _environment(self, pk):
        return Environment.objects.select_related("upper_machine").get(pk=pk)

    @action(detail=True, methods=["get"], url_path="tree")
    def tree(self, request, pk=None):
        try:
            environment = self._environment(pk)
            logger.info("cpd.api.tree.start environment=%s", environment.id)
            return Response(scan_report_tree(environment))
        except Environment.DoesNotExist:
            return Response({"message": "环境不存在。"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as exc:
            logger.exception("cpd.api.tree.failed environment=%s", pk)
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=True, methods=["get"], url_path="snapshot")
    def snapshot(self, request, pk=None):
        try:
            environment = self._environment(pk)
            logger.info("cpd.api.snapshot.start environment=%s", environment.id)
            refresh = str(request.query_params.get("refresh", "")).lower() in {"1", "true", "yes"}
            return Response(load_report_snapshot(environment, refresh=refresh))
        except Environment.DoesNotExist:
            return Response({"message": "环境不存在。"}, status=status.HTTP_404_NOT_FOUND)
        except Exception as exc:
            logger.exception("cpd.api.snapshot.failed environment=%s", pk)
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=True, methods=["get"], url_path="dataset")
    def dataset(self, request, pk=None):
        try:
            environment = self._environment(pk)
            refresh = str(request.query_params.get("refresh", "")).lower() in {"1", "true", "yes"}
            logger.info("cpd.api.dataset.start environment=%s refresh=%s", environment.id, refresh)
            return Response(load_report_dataset(
                environment,
                start_time=request.query_params.get("start_time", ""),
                end_time=request.query_params.get("end_time", ""),
                refresh=refresh,
            ))
        except Environment.DoesNotExist:
            return Response({"message": "环境不存在。"}, status=status.HTTP_404_NOT_FOUND)
        except (TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("cpd.api.dataset.failed environment=%s", pk)
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=True, methods=["get"], url_path="query")
    def query(self, request, pk=None):
        try:
            environment = self._environment(pk)
            logger.info("cpd.api.query.start environment=%s", environment.id)
            return Response(query_report_summaries(
                environment,
                page=int(request.query_params.get("page", "1")),
                page_size=int(request.query_params.get("page_size", "100")),
                start_time=request.query_params.get("start_time", ""),
                end_time=request.query_params.get("end_time", ""),
                subsystem=request.query_params.get("subsystem", "").strip(),
                module=request.query_params.get("module", "").strip(),
                file_name=request.query_params.get("file", "").strip(),
                result=request.query_params.get("result", "").strip(),
                validation=request.query_params.get("validation", "").strip(),
                quality=request.query_params.get("quality", "").strip(),
                mcs=request.query_params.get("mcs", "").strip(),
                duration=request.query_params.get("duration", "").strip(),
                query=request.query_params.get("q", "").strip(),
            ))
        except Environment.DoesNotExist:
            return Response({"message": "环境不存在。"}, status=status.HTTP_404_NOT_FOUND)
        except (TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("cpd.api.query.failed environment=%s", pk)
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=True, methods=["get"], url_path="reports")
    def reports(self, request, pk=None):
        try:
            environment = self._environment(pk)
            subsystem = request.query_params.get("subsystem", "").strip()
            module = request.query_params.get("module", "").strip()
            page = int(request.query_params.get("page", "1"))
            page_size = int(request.query_params.get("page_size", "100"))
            if not subsystem and not module:
                return Response(list_all_reports(environment, page=page, page_size=page_size))
            if not subsystem or not module:
                raise ValueError("子系统和模块必须同时提供。")
            return Response(list_reports(environment, subsystem, module, page=page, page_size=page_size))
        except Environment.DoesNotExist:
            return Response({"message": "环境不存在。"}, status=status.HTTP_404_NOT_FOUND)
        except (TypeError, ValueError) as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("cpd.api.reports.failed environment=%s", pk)
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    @action(detail=True, methods=["get"], url_path="data-files")
    def data_files(self, request, pk=None):
        try:
            q = request.query_params
            return Response(find_cpd_excel_files(self._environment(pk), q.get("subsystem", ""), q.get("module", ""), q.get("start_time", ""), q.get("end_time", "")))
        except Environment.DoesNotExist:
            return Response({"message": "环境不存在"}, status=404)
        except ValueError as exc:
            return Response({"message": str(exc)}, status=400)
        except Exception as exc:
            return Response({"message": str(exc)}, status=502)

    @action(detail=True, methods=["get"], url_path="excel-preview")
    def excel_preview(self, request, pk=None):
        try:
            q = request.query_params
            return Response(preview_cpd_excel(self._environment(pk), q.get("subsystem", ""), q.get("module", ""), q.get("path", ""), q.get("sheet", ""), q.get("start_time", ""), q.get("end_time", ""), page=q.get("page", 1), page_size=q.get("page_size", 100), sort_column=q.get("sort_column"), descending=q.get("descending") == "true"))
        except Environment.DoesNotExist:
            return Response({"message": "环境不存在"}, status=404)
        except ValueError as exc:
            return Response({"message": str(exc)}, status=400)
        except Exception as exc:
            return Response({"message": str(exc)}, status=502)

    @action(detail=True, methods=["get"], url_path="content")
    def content(self, request, pk=None):
        try:
            environment = self._environment(pk)
            return Response(read_report_content(
                environment,
                request.query_params.get("subsystem", ""),
                request.query_params.get("module", ""),
                request.query_params.get("file", ""),
            ))
        except Environment.DoesNotExist:
            return Response({"message": "环境不存在。"}, status=status.HTTP_404_NOT_FOUND)
        except ValueError as exc:
            return Response({"message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("cpd.api.content.failed environment=%s", pk)
            return Response({"message": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

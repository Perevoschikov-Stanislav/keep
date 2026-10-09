"use client";

import {
  ChevronDoubleLeftIcon,
  ChevronDoubleRightIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  TableCellsIcon,
} from "@heroicons/react/16/solid";
import { Button, Text } from "@tremor/react";
import type { Table } from "@tanstack/react-table";
import { INCIDENT_PAGINATION_OPTIONS } from "@/entities/incidents/model/models";

type Props = {
  table: Table<any>;
  // TODO: Add refresh button
  // allowRefresh?: boolean;
};

export function TablePagination({ table }: Props) {
  const pageIndex = table.getState().pagination.pageIndex;
  const pageCount = table.getPageCount();

  return (
    <div className="flex justify-between items-center">
      <Text>
        {pageCount ? (
          <>
            Showing {pageCount === 0 ? 0 : pageIndex + 1} of {pageCount}
          </>
        ) : null}
      </Text>
      <div className="flex gap-1">
        {/* Keep SSR pagination independent of Emotion's inline style insertion. */}
        <div className="relative">
          <select
            aria-label="Rows per page"
            className="rounded-lg border border-gray-200 bg-white py-2 pl-3 pr-12 text-sm focus:border-orange-500 focus:outline-none focus:ring-1 focus:ring-orange-500"
            value={table.getState().pagination.pageSize}
            onChange={(event) => table.setPageSize(Number(event.target.value))}
          >
            {INCIDENT_PAGINATION_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <TableCellsIcon className="pointer-events-none absolute right-6 top-1/2 h-4 w-4 -translate-y-1/2" />
        </div>
        <div className="flex">
          <Button
            className="pagination-button"
            icon={ChevronDoubleLeftIcon}
            onClick={() => table.setPageIndex(0)}
            disabled={!table.getCanPreviousPage()}
            size="xs"
            color="gray"
            variant="secondary"
          />
          <Button
            className="pagination-button"
            icon={ChevronLeftIcon}
            onClick={table.previousPage}
            disabled={!table.getCanPreviousPage()}
            size="xs"
            color="gray"
            variant="secondary"
          />
          <Button
            className="pagination-button"
            icon={ChevronRightIcon}
            onClick={table.nextPage}
            disabled={!table.getCanNextPage()}
            size="xs"
            color="gray"
            variant="secondary"
          />
          <Button
            className="pagination-button"
            icon={ChevronDoubleRightIcon}
            onClick={() => table.setPageIndex(pageCount - 1)}
            disabled={!table.getCanNextPage()}
            size="xs"
            color="gray"
            variant="secondary"
          />
        </div>
        {/* TODO: Add refresh button */}
      </div>
    </div>
  );
}

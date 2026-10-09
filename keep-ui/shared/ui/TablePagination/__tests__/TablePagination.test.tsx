import React, { act } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { renderToString } from "react-dom/server.node";
import { hydrateRoot } from "react-dom/client";
import { getCoreRowModel, useReactTable } from "@tanstack/react-table";
import { TablePagination } from "../TablePagination";

function Pagination() {
  const table = useReactTable<{ id: string }>({
    data: [],
    columns: [],
    rowCount: 45,
    manualPagination: true,
    getCoreRowModel: getCoreRowModel(),
    initialState: { pagination: { pageIndex: 1, pageSize: 20 } },
  });
  return <TablePagination table={table} />;
}

describe("TablePagination", () => {
  it("server-renders the selected page size and its options", () => {
    const container = document.createElement("div");
    container.innerHTML = renderToString(<Pagination />);
    const select = container.querySelector<HTMLSelectElement>("select");

    expect(select).not.toBeNull();
    expect(select).toHaveAttribute("aria-label", "Rows per page");
    expect(select?.value).toBe("20");
    expect(select?.options.length).toBeGreaterThan(1);
    expect(container.querySelector("style[data-emotion]")).toBeNull();
  });

  it("changes the page size using the table's pagination state", () => {
    render(<Pagination />);
    expect(screen.getByText("Showing 2 of 3")).toBeInTheDocument();

    fireEvent.change(screen.getByRole("combobox", { name: "Rows per page" }), {
      target: { value: "50" },
    });

    expect(screen.getByRole("combobox", { name: "Rows per page" })).toHaveValue("50");
    expect(screen.getByText("Showing 1 of 1")).toBeInTheDocument();
  });

  it("hydrates the server control without replacing it or reporting a mismatch", async () => {
    const container = document.createElement("div");
    container.innerHTML = renderToString(<Pagination />);
    document.body.appendChild(container);
    const serverControl = container.querySelector("select");
    const onRecoverableError = jest.fn();
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => {});
    let root: ReturnType<typeof hydrateRoot> | undefined;

    try {
      await act(async () => {
        root = hydrateRoot(container, <Pagination />, { onRecoverableError });
      });
      expect(onRecoverableError).not.toHaveBeenCalled();
      expect(consoleError).not.toHaveBeenCalled();
      expect(serverControl).not.toBeNull();
      expect(container.querySelector("select")).toBe(serverControl);
      fireEvent.change(screen.getByRole("combobox", { name: "Rows per page" }), {
        target: { value: "50" },
      });
      expect(screen.getByText("Showing 1 of 1")).toBeInTheDocument();
    } finally {
      await act(async () => root?.unmount());
      container.remove();
      consoleError.mockRestore();
    }
  });
});

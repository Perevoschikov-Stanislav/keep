import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { EnrichmentEditableField } from "../EnrichmentEditableField";

jest.mock("@/components/ui", () => ({
  Button: ({ tooltip, onClick, disabled }: any) => (
    <button aria-label={tooltip} onClick={onClick} disabled={disabled} />
  ),
}));
jest.mock("@tremor/react", () => ({
  Badge: ({ children }: any) => <span>{children}</span>,
  Icon: () => null,
  TextInput: ({ error, errorMessage, ...props }: any) => (
    <>
      <input {...props} aria-invalid={error} />
      {error && <span role="alert">{errorMessage}</span>}
    </>
  ),
}));

function openAddForm(isNameReadOnly: (name: string) => boolean) {
  const onUpdate = jest.fn();
  render(
    <EnrichmentEditableField
      value=""
      onUpdate={onUpdate}
      isNameReadOnly={isNameReadOnly}
    />
  );
  fireEvent.click(screen.getByText("Add new field"));
  fireEvent.change(screen.getByPlaceholderText("Add value"), {
    target: { value: "new value" },
  });
  return onUpdate;
}

it("blocks a protected existing key in the add form", () => {
  const onUpdate = openAddForm((name) => name.toLowerCase() === "ticket");
  fireEvent.change(screen.getByPlaceholderText("Add name"), {
    target: { value: " Ticket " },
  });
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  expect(screen.getByRole("alert")).toHaveTextContent("editable field name");
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(onUpdate).not.toHaveBeenCalled();
});

it("blocks keys protected by a configured prefix", () => {
  const onUpdate = openAddForm((name) => name.startsWith("integration_"));
  fireEvent.change(screen.getByPlaceholderText("Add name"), {
    target: { value: "integration_url" },
  });
  expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  expect(onUpdate).not.toHaveBeenCalled();
});

it("allows an editable key after correcting a protected name", () => {
  const onUpdate = openAddForm((name) => name === "ticket");
  const input = screen.getByPlaceholderText("Add name");
  fireEvent.change(input, { target: { value: "ticket" } });
  fireEvent.change(input, { target: { value: "note" } });
  const save = screen.getByRole("button", { name: "Save" });
  expect(save).toBeEnabled();
  fireEvent.click(save);
  expect(onUpdate).toHaveBeenCalledWith("note", "new value");
});

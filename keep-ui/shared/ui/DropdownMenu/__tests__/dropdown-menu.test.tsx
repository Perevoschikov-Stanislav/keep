import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { DropdownMenu } from "../DropdownMenu";

it("keeps the menu open after the trigger click and closes it after choosing an action", async () => {
  const silence = jest.fn();
  render(<DropdownMenu.Menu label="Actions"><DropdownMenu.Item label="Silence" onClick={silence} /></DropdownMenu.Menu>);
  const trigger = screen.getByRole("button", { name: "Actions" });
  fireEvent.mouseDown(trigger);
  fireEvent.click(trigger);
  const item = await screen.findByRole("menuitem", { name: "Silence" });
  fireEvent.click(item);
  expect(silence).toHaveBeenCalledTimes(1);
  await waitFor(() => expect(screen.queryByRole("menuitem", { name: "Silence" })).not.toBeInTheDocument());
});

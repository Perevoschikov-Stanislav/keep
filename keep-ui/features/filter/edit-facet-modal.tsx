import React, { useEffect, useState } from "react";
import { TextInput } from "@tremor/react";
import Modal from "@/components/ui/Modal";
import { FacetDto, UpdateFacetDto } from "./models";
import { Button } from "@/components/ui";

interface EditFacetModalProps {
  facet: FacetDto | null;
  isOpen: boolean;
  onClose: () => void;
  onUpdateFacet: (facetId: string, updatedFacet: UpdateFacetDto) => void;
}

export const EditFacetModal: React.FC<EditFacetModalProps> = ({
  facet,
  isOpen,
  onClose,
  onUpdateFacet,
}) => {
  const [name, setName] = useState("");
  const [propertyPath, setPropertyPath] = useState("");

  useEffect(() => {
    if (facet) {
      setName(facet.name || "");
      setPropertyPath(facet.property_path || "");
    }
  }, [facet]);

  const handleUpdate = () => {
    if (!facet) return;
    onUpdateFacet(facet.id, {
      property_path: propertyPath.trim(),
      name: name.trim(),
    });
    onClose();
  };

  function isSubmitEnabled(): boolean {
    return name.trim().length > 0 && propertyPath.trim().length > 0;
  }

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Edit Facet"
      className="w-[400px]"
    >
      <div className="mt-3 max-h-96 overflow-auto space-y-1">
        <div>
          <div className="mb-1">
            <span className="font-bold">Facet name:</span>
          </div>

          <TextInput
            placeholder="Enter facet name"
            required={true}
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="mb-4"
          />
        </div>
        <div>
          <div className="mb-1">
            <span className="font-bold">Facet property path:</span>
          </div>

          <TextInput
            placeholder="Enter facet property path"
            required={true}
            value={propertyPath}
            onChange={(e) => setPropertyPath(e.target.value)}
            className="mb-4"
          />
        </div>
      </div>
      <div className="flex flex-1 justify-end gap-2">
        <Button
          data-testid="cancel-facet-edit-btn"
          color="orange"
          size="xs"
          variant="secondary"
          onClick={onClose}
        >
          Cancel
        </Button>
        <Button
          data-testid="save-facet-btn"
          color="orange"
          size="xs"
          variant="primary"
          type="submit"
          disabled={!isSubmitEnabled()}
          onClick={handleUpdate}
        >
          Save
        </Button>
      </div>
    </Modal>
  );
};

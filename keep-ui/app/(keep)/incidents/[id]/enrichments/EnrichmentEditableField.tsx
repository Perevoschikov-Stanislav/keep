import { useRouter } from "next/navigation";
import React, { useState } from "react";
import { xor } from "lodash";
import { Badge, Icon, TextInput } from "@tremor/react";
import { Button } from "@/components/ui";
import { FiExternalLink, FiLock, FiSave, FiTrash2, FiX } from "react-icons/fi";
import { MdModeEdit } from "react-icons/md";

interface EnrichmentEditableFieldProps {
  name?: string;
  value: string | string[];
  onUpdate: (fieldName: string, newValue: string | string[]) => void;
  onDelete?: (fieldName: string) => void;
  children?: React.ReactNode;
  readOnly?: boolean;
}

const isUrl = (str: string): boolean => {
  try {
    const url = new URL(str);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
};

const renderStringWithLinks = (
  text: string,
  field?: string,
  onBadgeClick?: (val: string) => void
) => {
  const trimmed = text.trim();
  if (isUrl(trimmed)) {
    return (
      <a
        href={trimmed}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded-md bg-orange-50 text-orange-700 hover:bg-orange-100 hover:text-orange-900 border border-orange-200 transition-colors cursor-pointer"
        onClick={(e) => e.stopPropagation()}
      >
        <FiExternalLink className="w-3.5 h-3.5 text-orange-600 flex-shrink-0" />
        <span className="truncate max-w-xs">{trimmed}</span>
      </a>
    );
  }

  const urlRegex = /(https?:\/\/[^\s]+)/g;
  if (urlRegex.test(trimmed)) {
    const parts = trimmed.split(urlRegex);
    return (
      <span className="text-sm text-gray-800 break-words">
        {parts.map((part, i) => {
          if (isUrl(part)) {
            return (
              <a
                key={i}
                href={part}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-orange-600 hover:text-orange-800 hover:underline font-medium mx-1"
                onClick={(e) => e.stopPropagation()}
              >
                {part}
                <FiExternalLink className="w-3 h-3 inline flex-shrink-0" />
              </a>
            );
          }
          return <span key={i}>{part}</span>;
        })}
      </span>
    );
  }

  if (onBadgeClick && field) {
    return (
      <Badge
        key={trimmed}
        color="orange"
        size="sm"
        className="cursor-pointer"
        onClick={() => onBadgeClick(trimmed)}
      >
        {trimmed}
      </Badge>
    );
  }

  return <span className="text-sm text-gray-800 break-words">{trimmed}</span>;
};

export const EnrichmentEditableField = ({
  name,
  value,
  onUpdate,
  onDelete,
  children,
  readOnly = false,
}: EnrichmentEditableFieldProps) => {
  const router = useRouter();

  const [editMode, setEditMode] = useState(false);
  const [stringedValue, setStringedValue] = useState(
    Array.isArray(value) ? value.join(", ") : value.toString()
  );
  const [fieldName, setFieldName] = useState<string>(name || "");
  const [fieldNameError, setFieldNameError] = useState<boolean>(false);
  const [valueError, setValueError] = useState<boolean>(false);

  const handleSave = async () => {
    const newValue = Array.isArray(value)
      ? stringedValue.split(",").map((s) => s.trim())
      : stringedValue.toString().trim();

    if (Array.isArray(newValue) && xor(value, newValue).length === 0) {
      return;
    } else if (value == newValue) {
      return;
    }

    onUpdate(fieldName, newValue);
    setEditMode(false);

    // reset if this is add form
    resetForm();
  };

  const handleUnenrich = async () => {
    if (onDelete) {
      onDelete(fieldName);
    }
    setEditMode(false);
  };

  const handleCancel = () => {
    // Reset value
    setEditMode(false);
    resetForm();
  };

  const resetForm = () => {
    setStringedValue(Array.isArray(value) ? value.join(", ") : value);
    setFieldName(name || "");
  };

  const filterBy = (key: string, value: string) => {
    router.push(
      `/alerts/feed?cel=${key}%3D%3D${encodeURIComponent(`"${value}"`)}`
    );
  };

  const handleNameChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setFieldNameError(e.target.value === "");
    setFieldName(e.target.value);
  };

  const handleValueChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setValueError(e.target.value === "");
    setStringedValue(e.target.value);
  };

  if (editMode) {
    return (
      <div className="flex items-center flex-wrap gap-2.5 z-50">
        {!name && (
          <TextInput
            value={fieldName}
            error={fieldNameError}
            onChange={handleNameChange}
            placeholder="Add name"
          />
        )}
        <TextInput
          value={stringedValue}
          error={valueError}
          onChange={handleValueChange}
          placeholder="Add value"
        />
        <Button
          className="leading-none p-2 rounded-md"
          variant="secondary"
          disabled={!(fieldName && stringedValue)}
          tooltip="Save"
          icon={() => (
            <Icon icon={FiSave} className={`w-4 h-4 text-orange-500`} />
          )}
          onClick={handleSave}
        />
        <Button
          className="leading-none p-2 rounded-md"
          variant="destructive"
          tooltip="Cancel"
          icon={FiX}
          onClick={handleCancel}
        />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1 relative">
      {name ? (
        <div className="flex flex-wrap gap-1 group items-center">
          {children
            ? children
            : value != null && value.length > 0
              ? !Array.isArray(value)
                ? renderStringWithLinks(
                    value.toString(),
                    fieldName,
                    (v) => filterBy(fieldName, v)
                  )
                : value.map((item: string) => (
                    <React.Fragment key={item}>
                      {renderStringWithLinks(
                        item,
                        fieldName,
                        (v) => filterBy(fieldName, v)
                      )}
                    </React.Fragment>
                  ))
              : `No data for ${name}`}

          {!readOnly && (
            <Button
              variant="light"
              className="text-gray-500 leading-none p-2 rounded-md prevent-row-click hover:bg-slate-200 [&>[role='tooltip']]:z-50 transition-opacity duration-100 opacity-0 group-hover:opacity-100"
              tooltip="Edit"
              onClick={() => setEditMode(true)}
              icon={() => (
                <Icon icon={MdModeEdit} className={`w-4 h-4 text-orange-500`} />
              )}
            />
          )}

          {!readOnly && onDelete && (
            <Button
              variant="light"
              className="text-gray-500 leading-none p-2 rounded-md prevent-row-click hover:bg-slate-200 [&>[role='tooltip']]:z-50 transition-opacity duration-100 opacity-0 group-hover:opacity-100"
              tooltip="Un-enrich"
              onClick={handleUnenrich}
              icon={() => (
                <Icon icon={FiTrash2} className={`w-4 h-4 text-red-500`} />
              )}
            />
          )}

          {readOnly && (
            <span
              className="text-gray-400 p-1 opacity-0 group-hover:opacity-100 transition-opacity duration-100"
              title="Protected field (read-only)"
            >
              <FiLock className="w-3.5 h-3.5 inline" />
            </span>
          )}
        </div>
      ) : (
        <div
          className="flex gap-2 items-center cursor-pointer"
          onClick={() => setEditMode(true)}
        >
          <Badge>+</Badge> Add new field
        </div>
      )}
    </div>
  );
};

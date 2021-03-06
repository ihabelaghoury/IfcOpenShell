import ifcopenshell
from blenderbim.bim.module.owner.api import create_owner_history
from blenderbim.bim.module.owner.api import update_owner_history


class Usecase:
    def __init__(self, file, settings=None):
        self.file = file
        self.settings = {
            "product": None,
            "structural_analysis_model": None,
        }
        for key, value in settings.items():
            self.settings[key] = value

    def execute(self):
        if not self.settings["structural_analysis_model"].IsGroupedBy:
            return self.file.create_entity("IfcRelAssignsToGroup", **{
                "GlobalId": ifcopenshell.guid.new(),
                "OwnerHistory": create_owner_history(),
                "RelatedObjects": [self.settings["product"]],
                "RelatingGroup": self.settings["structural_analysis_model"]
            })
        rel = self.settings["structural_analysis_model"].IsGroupedBy[0]
        related_objects = set(rel.RelatedObjects) or set()
        related_objects.add(self.settings["product"])
        rel.RelatedObjects = list(related_objects)
        update_owner_history(rel)

from . import ifcopenshell
from .ifcopenshell import geom
import bpy
import os
import json
import time
import mathutils
from .helper import SIUnitHelper

cwd = os.path.dirname(os.path.realpath(__file__)) + os.path.sep

class IfcSchema():
    def __init__(self):
        with open('{}ifc_elements_IFC4.json'.format(cwd + 'schema/')) as f:
            self.elements = json.load(f)

ifc_schema = IfcSchema()

class MaterialCreator():
    def __init__(self):
        self.mesh = None
        self.materials = {}

    def create(self, element, object, mesh):
        self.object = object
        self.mesh = mesh
        if not element.Representation:
            return
        for item in element.Representation.Representations[0].Items:
            if item.StyledByItem:
                styled_item = item.StyledByItem[0]
                material_name = str(styled_item.Name if styled_item.Name else styled_item.id())
                if material_name not in self.materials:
                    self.materials[material_name] = bpy.data.materials.new(material_name)
                self.parse_styled_item(item.StyledByItem[0], self.materials[material_name])
                self.assign_material_to_mesh(self.materials[material_name], is_styled_item=True)
                return # styled items override material styles
        for association in element.HasAssociations:
            if association.is_a('IfcRelAssociatesMaterial'):
                material_select = association.RelatingMaterial
                if material_select.is_a('IfcMaterialDefinition'):
                    self.create_definition(material_select)

    def create_definition(self, material):
        if material.is_a('IfcMaterial'):
            self.create_single(material)

    def create_single(self, material):
        if material.Name not in self.materials:
            self.create_new_single(material)
        return self.assign_material_to_mesh(self.materials[material.Name])

    def create_new_single(self, material):
        self.materials[material.Name] = bpy.data.materials.new(material.Name)
        if not material.HasRepresentation \
            or not material.HasRepresentation[0].Representations:
            return
        for representation in material.HasRepresentation[0].Representations:
            if not representation.Items:
                continue
            for item in representation.Items:
                if not item.is_a('IfcStyledItem'):
                    continue
                self.parse_styled_item(item, self.materials[material.Name])

    def parse_styled_item(self, styled_item, material):
        for style in styled_item.Styles:
            # Note IfcPresentationStyleAssignment is deprecated as of IFC4,
            # but we still support it as it is widely used
            if style.is_a('IfcPresentationStyleAssignment'):
                style = style.Styles[0]
            if not style.is_a('IfcSurfaceStyle'):
                continue
            external_style = None
            for surface_style in style.Styles:
                if surface_style.is_a('IfcSurfaceStyleShading'):
                    alpha = 1.
                    if surface_style.Transparency:
                        alpha = 1 - surface_style.Transparency
                    material.diffuse_color = (
                        surface_style.SurfaceColour.Red,
                        surface_style.SurfaceColour.Green,
                        surface_style.SurfaceColour.Blue,
                        alpha)
                elif surface_style.is_a('IfcExternallyDefinedSurfaceStyle'):
                    external_style = surface_style
            if external_style:
                material.BIMMaterialProperties.is_external = True
                material.BIMMaterialProperties.location = external_style.Location
                material.BIMMaterialProperties.identification = external_style.Identification
                material.BIMMaterialProperties.name = external_style.Name

    def assign_material_to_mesh(self, material, is_styled_item=False):
        self.mesh.materials.append(material)
        if is_styled_item:
            self.object.material_slots[0].link = 'OBJECT'
            self.object.material_slots[0].material = material

class IfcImporter():
    def __init__(self, ifc_import_settings):
        self.ifc_import_settings = ifc_import_settings
        self.diff = None
        self.file = None
        self.settings = ifcopenshell.geom.settings()
        if self.ifc_import_settings.should_import_curves:
            self.settings.set(self.settings.INCLUDE_CURVES, True)
        self.project = None
        self.spatial_structure_elements = {}
        self.elements = {}
        self.meshes = {}
        self.mesh_shapes = {}
        self.time = 0
        self.unit_scale = 1

        self.material_creator = MaterialCreator()

    def execute(self):
        self.load_diff()
        self.load_file()
        self.calculate_unit_scale()
        self.create_project()
        self.create_spatial_hierarchy()
        self.purge_diff()
        elements = self.file.by_type('IfcElement') + self.file.by_type('IfcSpace')
        for element in elements:
            self.create_object(element)

    def load_diff(self):
        if not self.ifc_import_settings.diff_file:
            return
        with open(self.ifc_import_settings.diff_file, 'r') as file:
            self.diff = json.load(file)

    def load_file(self):
        print('loading file {}'.format(self.ifc_import_settings.input_file))
        self.file = ifcopenshell.open(self.ifc_import_settings.input_file)

    def calculate_unit_scale(self):
        units = self.file.by_type('IfcUnitAssignment')[0]
        for unit in units.Units:
            if not hasattr(unit, 'UnitType') \
                or unit.UnitType != 'LENGTHUNIT':
                continue
            while unit.is_a('IfcConversionBasedUnit'):
                self.unit_scale *= unit.ConversionFactor.ValueComponent.wrappedValue
                unit = unit.ConversionFactor.UnitComponent
            if unit.is_a('IfcSIUnit'):
                self.unit_scale *= SIUnitHelper.get_prefix_multiplier(unit.Prefix)

    def create_project(self):
        self.project = { 'ifc': self.file.by_type('IfcProject')[0] }
        self.project['blender'] = bpy.data.collections.new('IfcProject/{}'.format(self.project['ifc'].Name))
        bpy.context.scene.collection.children.link(self.project['blender'])

    def create_spatial_hierarchy(self):
        elements = self.file.by_type('IfcSite') + self.file.by_type('IfcBuilding') + self.file.by_type('IfcBuildingStorey')
        attempts = 0
        while len(self.spatial_structure_elements) < len(elements) \
            and attempts <= len(elements):
            for element in elements:
                name = self.get_name(element)
                if name in self.spatial_structure_elements:
                    continue
                # Occurs when some naughty programs export IFC site objects
                if not element.Decomposes:
                    continue
                parent = element.Decomposes[0].RelatingObject
                parent_name = self.get_name(parent)
                if parent.is_a('IfcProject'):
                    self.spatial_structure_elements[name] = {
                        'blender': bpy.data.collections.new(name)}
                    self.project['blender'].children.link(self.spatial_structure_elements[name]['blender'])
                elif parent_name in self.spatial_structure_elements:
                    self.spatial_structure_elements[name] = {
                        'blender': bpy.data.collections.new(name)}
                    self.spatial_structure_elements[parent_name]['blender'].children.link(
                        self.spatial_structure_elements[name]['blender'])
            attempts += 1

    def get_name(self, element):
        return '{}/{}'.format(element.is_a(), element.Name)

    def purge_diff(self):
        objects_to_purge = []
        for obj in bpy.data.objects:
            if 'GlobalId' not in obj.BIMObjectProperties.attributes:
                continue
            global_id = obj.BIMObjectProperties.attributes['GlobalId'].string_value
            if global_id in self.diff['deleted'] \
                or global_id in self.diff['changed'].keys():
                objects_to_purge.append(obj)
        bpy.ops.object.delete({'selected_objects': objects_to_purge})

    def create_object(self, element):
        if self.diff:
            if element.GlobalId not in self.diff['added'] \
                and element.GlobalId not in self.diff['changed'].keys():
                return

        print('Creating object {}'.format(element))
        self.time = time.time()
        if element.is_a('IfcOpeningElement'):
            return

        try:
            representation_id = self.get_representation_id(element)

            mesh_name = 'mesh-{}'.format(representation_id)
            mesh = self.meshes.get(mesh_name)
            if mesh is None \
                or representation_id is None:
                shape = ifcopenshell.geom.create_shape(self.settings, element)
                print('Shape was generated in {:.2f}'.format(time.time() - self.time))
                self.time = time.time()

                mesh = self.create_mesh(element, shape)
                self.meshes[mesh_name] = mesh
                self.mesh_shapes[mesh_name] = shape
            else:
                print('MESH REUSED')
        except:
            print('Failed to generate shape for {}'.format(element))
            return

        object = bpy.data.objects.new(self.get_name(element), mesh)
        self.material_creator.create(element, object, mesh)

        element_matrix = self.get_local_placement(element.ObjectPlacement)

        # Blender supports reusing a mesh with a different transformation
        # applied at the object level. In contrast, IFC supports reusing a mesh
        # with a different transformation applied at the mesh level _as well as_
        # the object level. For this reason, if the end-goal is to re-use mesh
        # data, we must combine IFC's mesh-level transformation into Blender's
        # object level transformation.

        # The first step to do this is to _undo_ the mesh-level transformation
        # from whatever shared mesh we are using, as it is not necessarily the
        # same as the current mesh.
        shared_shape_transformation = self.get_representation_cartesian_transformation(
            self.file.by_id(self.mesh_shapes[mesh_name].product.id()))
        if shared_shape_transformation:
            shared_transform = self.get_cartesiantransformationoperator(shared_shape_transformation)
            shared_transform.invert()
            element_matrix = element_matrix @ shared_transform

        # The next step is to apply the current element's mesh level
        # transformation to our current element's object transformation
        transformation = self.get_representation_cartesian_transformation(element)
        if transformation:
            element_matrix = self.get_cartesiantransformationoperator(transformation) @ element_matrix

        element_matrix[0][3] *= self.unit_scale
        element_matrix[1][3] *= self.unit_scale
        element_matrix[2][3] *= self.unit_scale

        object.matrix_world = element_matrix # element_matrix gives wrong results

        attributes = element.get_info()
        if element.is_a() in ifc_schema.elements:
            applicable_attributes = [a['name'] for a in ifc_schema.elements[element.is_a()]['attributes']]
            for key, value in attributes.items():
                if key not in applicable_attributes \
                    or value is None:
                    continue
                attribute = object.BIMObjectProperties.attributes.add()
                attribute.name = key
                attribute.data_type = 'string'
                attribute.string_value = str(value)

        if hasattr(element, 'ContainedInStructure') \
            and element.ContainedInStructure \
            and element.ContainedInStructure[0].RelatingStructure:
            structure_name = self.get_name(element.ContainedInStructure[0].RelatingStructure)
            if structure_name in self.spatial_structure_elements:
                self.spatial_structure_elements[structure_name]['blender'].objects.link(object)
        else:
            print('Warning: this object is outside the spatial hierarchy')
            bpy.context.scene.collection.objects.link(object)

    def get_representation_id(self, element):
        if not element.Representation:
            return None
        for representation in element.Representation.Representations:
            if not representation.is_a('IfcShapeRepresentation'):
                continue
            if representation.RepresentationIdentifier == 'Body' \
                and representation.RepresentationType != 'MappedRepresentation':
                return representation.id()
            elif representation.RepresentationIdentifier == 'Body':
                return representation.Items[0].MappingSource.MappedRepresentation.id()

    def get_representation_cartesian_transformation(self, element):
        if not element.Representation:
            return None
        for representation in element.Representation.Representations:
            if not representation.is_a('IfcShapeRepresentation'):
                continue
            if representation.RepresentationIdentifier == 'Body' \
                and representation.RepresentationType == 'MappedRepresentation':
                return representation.Items[0].MappingTarget

    def create_mesh(self, element, shape):
        try:
            mesh = bpy.data.meshes.new(shape.geometry.id)
            f = shape.geometry.faces
            e = shape.geometry.edges
            v = shape.geometry.verts
            vertices = [[v[i], v[i + 1], v[i + 2]]
                     for i in range(0, len(v), 3)]
            faces = [[f[i], f[i + 1], f[i + 2]]
                     for i in range(0, len(f), 3)]
            if faces:
                edges = []
            else:
                edges = [[e[i], e[i + 1]]
                         for i in range(0, len(e), 2)]
            mesh.from_pydata(vertices, edges, faces)
            return mesh
        except:
            print('Could not create mesh for {}: {}/{}'.format(
                element.GlobalId, self.get_name(element)))

    def a2p(self, o, z, x):
        y = z.cross(x)
        r = mathutils.Matrix((x, y, z, o))
        r.resize_4x4()
        r.transpose()
        return r
        
    def get_axis2placement(self, plc): 
        z = mathutils.Vector(plc.Axis.DirectionRatios if plc.Axis else (0,0,1)) 
        x = mathutils.Vector(plc.RefDirection.DirectionRatios if plc.RefDirection else (1,0,0)) 
        o = plc.Location.Coordinates 
        return self.a2p(o,z,x) 

    def get_cartesiantransformationoperator(self, plc): 
        #z = mathutils.Vector(plc.Axis3.DirectionRatios if plc.Axis3 else (0,0,1)) 
        x = mathutils.Vector(plc.Axis1.DirectionRatios if plc.Axis1 else (1,0,0)) 
        z = x.cross(mathutils.Vector(plc.Axis2.DirectionRatios if plc.Axis2 else (0,1,0)))
        o = plc.LocalOrigin.Coordinates 
        return self.a2p(o,z,x) 
        
    def get_local_placement(self, plc):
        if plc.PlacementRelTo is None: 
            parent = mathutils.Matrix()
        else:
            parent = self.get_local_placement(plc.PlacementRelTo)
        if self.ifc_import_settings.should_ignore_site_coordinates \
            and 'IfcSite' in [o.is_a() for o in plc.PlacesObject]:
            return parent
        return parent @ self.get_axis2placement(plc.RelativePlacement)

class IfcImportSettings:
    def __init__(self):
        self.logger = None
        self.input_file = None
        self.should_ignore_site_coordinates = False
        self.should_import_curves = False
        self.diff_file = None

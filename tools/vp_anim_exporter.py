bl_info = {
    "name": "Viva Piñata Animation Exporter",
    "author": "Viva Pinata Reverse Engineering",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "File > Export > Viva Piñata Animations (.json)",
    "description": "Export armature animations as JSON for game injection",
    "category": "Import-Export",
}

import bpy
import json
import os
from bpy.props import StringProperty, BoolProperty
from bpy_extras.io_utils import ExportHelper


class ExportVPAnim(bpy.types.Operator, ExportHelper):
    """Export armature animations for Viva Piñata game injection"""
    bl_idname = "export_scene.vp_anim"
    bl_label = "Export Viva Piñata Animations"
    bl_options = {'PRESET'}

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={'HIDDEN'})

    animation_filter: StringProperty(
        name="Filter",
        description="Only export animations containing this text (blank = all)",
        default=""
    )

    include_location: BoolProperty(
        name="Include Location",
        description="Also export bone locations (for root motion)",
        default=True
    )

    def execute(self, context):
        armature_obj = None
        for obj in context.scene.objects:
            if obj.type == 'ARMATURE':
                armature_obj = obj
                break

        if armature_obj is None:
            self.report({'ERROR'}, "No armature found in scene")
            return {'CANCELLED'}

        # Find target animations
        target_actions = []
        for action in bpy.data.actions:
            if self.animation_filter and self.animation_filter.lower() not in action.name.lower():
                continue
            target_actions.append(action)

        if not target_actions:
            self.report({'ERROR'}, f"No animations found matching '{self.animation_filter}'")
            return {'CANCELLED'}

        # Store current state
        original_action = None
        original_frame = context.scene.frame_current
        if armature_obj.animation_data and armature_obj.animation_data.action:
            original_action = armature_obj.animation_data.action

        bone_names = [b.name for b in armature_obj.data.bones if b.name != 'static']
        all_exports = []

        for action in target_actions:
            # Assign action
            armature_obj.animation_data_create()
            armature_obj.animation_data.action = action
            if hasattr(action, 'slots') and len(action.slots) > 0:
                armature_obj.animation_data.action_slot = action.slots[0]

            s = int(action.frame_range[0])
            e = int(action.frame_range[1])
            total = e - s + 1

            result = {
                "name": action.name,
                "frame_start": s,
                "frame_end": e,
                "total_frames": total,
                "fps": context.scene.render.fps,
                "bones": {}
            }

            for bone_name in bone_names:
                result["bones"][bone_name] = []

            # Sample each frame
            for frame in range(s, e + 1):
                context.scene.frame_set(frame)
                dg = context.evaluated_depsgraph_get()
                evaluated_arm = armature_obj.evaluated_get(dg)

                for bone_name in bone_names:
                    pb = evaluated_arm.pose.bones.get(bone_name)
                    if pb is None:
                        continue

                    entry = {
                        "frame": frame,
                        "rotation": [
                            pb.rotation_quaternion[0],
                            pb.rotation_quaternion[1],
                            pb.rotation_quaternion[2],
                            pb.rotation_quaternion[3]
                        ]
                    }

                    if self.include_location:
                        entry["location"] = [
                            pb.location[0],
                            pb.location[1],
                            pb.location[2]
                        ]

                    result["bones"][bone_name].append(entry)

            all_exports.append(result)
            self.report({'INFO'}, f"Exported: {action.name} ({total} frames, {len(bone_names)} bones)")

        # Restore original state
        if original_action:
            armature_obj.animation_data.action = original_action
        context.scene.frame_set(original_frame)

        # Save to file
        output = {
            "format": "vp_anim_v1",
            "armature": armature_obj.name,
            "bone_count": len(bone_names),
            "bones": bone_names,
            "animations": all_exports
        }

        with open(self.filepath, 'w') as f:
            json.dump(output, f)

        file_size = os.path.getsize(self.filepath)
        self.report({'INFO'}, f"Saved {len(all_exports)} animations to {self.filepath} ({file_size} bytes)")

        return {'FINISHED'}


def menu_func_export(self, context):
    self.layout.operator(ExportVPAnim.bl_idname, text="Viva Piñata Animations (.json)")


def register():
    bpy.utils.register_class(ExportVPAnim)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    bpy.utils.unregister_class(ExportVPAnim)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)


if __name__ == "__main__":
    register()

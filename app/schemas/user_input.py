class UserInput(BaseModel):
    purpose: Optional[str] = None
    duration: Optional[str] = None
    desired_region: Optional[str] = None
    region_style: Optional[str] = None
    desired_vibe: Optional[str] = None
    tourism_hobby: Optional[str] = None
    work_required: Optional[bool] = None
    work_style: Optional[str] = None
    transport: Optional[str] = None
    travel_distance: Optional[str] = None
    living_infra: Optional[str] = None
    budget: Optional[str] = None
    accommodation_style: Optional[str] = None
    companion: Optional[str] = None
    priority: Optional[str] = None
    additional_request: Optional[str] = None
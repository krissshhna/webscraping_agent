from pydantic import BaseModel, Field


class LicenseMetricDetail(BaseModel):
    """Structured representation of the LicenseMetric sub-object."""
    value: str = Field(default="N/A", alias="Value", description="How the product is licensed (e.g. 'Per Server', 'Per User', 'Per Core')")
    ai_curated: str = Field(default="", alias="AICurated", description="'Yes' if the metric was inferred by the AI, 'No' if explicitly stated on the page")
    citations: str = Field(default="", alias="Citations", description="Direct quote or reference from the page that supports the license metric")

    model_config = {"populate_by_name": True}


class ProductRecord(BaseModel):
    """
    Structured output schema for licensing-focused product extraction.
    Matches the fields: RawProduct, Vendor, Product, Edition, Version, LicenseMetric.
    """
    raw_product: str = Field(
        default="",
        alias="RawProduct",
        description="The exact product string or name as it appears in the raw context"
    )
    vendor: str = Field(
        default="",
        alias="Vendor",
        description="The standardized company or organization name that produces the item (e.g. 'Microsoft Corporation')"
    )
    product: str = Field(
        default="",
        alias="Product",
        description="The clean, core product name without edition or version text"
    )
    edition: str = Field(
        default="",
        alias="Edition",
        description="The specific product edition or tier (e.g. 'Standard (without Hyper-V)'). Empty string if none."
    )
    version: str = Field(
        default="",
        alias="Version",
        description="The major/minor version string or number (e.g. '6', '2019', '11')"
    )
    license_metric: str = Field(
        default="{\"Value\": \"N/A\", \"AICurated\": \"\", \"Citations\": \"\"}",
        alias="LicenseMetric",
        description=(
            "A JSON object string with keys 'Value', 'AICurated', and 'Citations' "
            "describing how the product is licensed. "
            "Example: '{\"Value\": \"Per Server\", \"AICurated\": \"Yes\", \"Citations\": \"...\"}'"
        )
    )
    eos: str = Field(
        default="N/A",
        alias="EOS",
        description="The End of Support (EOS) or mainstream support end date or text (e.g. '2026-10-13'). Use 'N/A' if not found."
    )
    eol: str = Field(
        default="N/A",
        alias="EOL",
        description="The End of Life (EOL), extended support end, or retirement date or text (e.g. '2031-10-14'). Use 'N/A' if not found."
    )

    model_config = {"populate_by_name": True}
